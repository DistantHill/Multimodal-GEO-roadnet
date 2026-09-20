"""Disk-backed Clip/Fitting preserves geometry, ordering and ownership."""
import math
import inspect
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from track.v2.clip import extract_clips, extract_clips_for_detect
from track.v2.clip_spool import DiskSequence
from track.v2.db.queries import fetch_resolved_clip_tracks
from track.v2.fitting import (
    _OrientedClip, _fit_pair_stream, _orient_clip, _resample_at_progress,
    _resample_at_spacing, _xy_polyline_length, fit_centerline_variants,
)
from track.v2.models import Centerline, ClipInput, ClipTrack, FittingConfig, RuntimeConfig
from track.v2.projection import CoordinateTransformer
from .test_clip import _CONTEXT, _point
from .test_fitting_network import _clip, _clip_result, _TRANSFORMER

_EPS = 1e-9


def test_disk_sequence_repeatability_lookup_slice_and_cleanup():
    with DiskSequence() as sequence:
        directory = Path(sequence._directory.name)
        for index in range(3):
            sequence.append((index, [index + 1]), key=str(index))
        assert list(sequence) == list(sequence)
        assert sequence[-1] == (2, [3])
        assert sequence[::-1] == ((2, [3]), (1, [2]), (0, [1]))
        assert sequence.get_by_clip_id("1") == sequence[1]
        with pytest.raises(IndexError):
            sequence[3]
    assert not directory.exists()


def test_streamed_clip_result_matches_memory_and_serializes(tmp_path):
    tracks = tuple(ClipTrack("v", f"s{i}", (_point(i * 2, -10), _point(i * 2 + 1, 10)), i)
                   for i in range(1, 4))
    run = SimpleNamespace(status="completed", refine_id="r", spatial_context=_CONTEXT)
    expected = extract_clips(ClipInput((), tracks, _CONTEXT, "detect"))
    reader = MagicMock()
    reader.__enter__.return_value = reader
    reader.intersections.return_value = ()
    reader.iter_tracks.return_value = iter(tracks)
    conn = MagicMock()
    with (
        patch("track.v2.clip.fetch_detect_row", return_value=run),
        patch("track.v2.clip.find_detect_artifact", return_value=SimpleNamespace(
            descriptor="descriptor", state="present",
        )),
        patch("track.v2.clip.DetectDatasetReader", return_value=reader),
    ):
        actual = extract_clips_for_detect(
            conn, "detect", publication_root="/parquet", data_root=tmp_path,
            runtime=RuntimeConfig(),
        )
    try:
        assert actual.as_dict() == expected.as_dict()
        assert tuple(actual.clips) == expected.clips
        assert tuple(actual.clips) == expected.clips
    finally:
        actual.clips.close()


@pytest.mark.parametrize("count", [2, 3, 9])
def test_streamed_pair_is_exact_reference_median(count, tmp_path):
    clips = [_clip(str(i), 1 if i % 2 == 0 else 2, 2 if i % 2 == 0 else 1,
                   [(0, i), (40, i * i), (200, -i)][::1 if i % 2 == 0 else -1])
             for i in range(count)]
    oriented = [_orient_clip(clip, _TRANSFORMER)[0] for clip in clips]
    cfg = FittingConfig()
    expected = _reference_fit_pair((1, 2), oriented, cfg, _TRANSFORMER)
    actual = _fit_pair_stream((1, 2), lambda: iter(oriented), count, cfg, _TRANSFORMER)
    assert actual == expected
    result = _clip_result(*clips)
    from track.v2.clip_relation import ClipRelation
    with ClipRelation(RuntimeConfig(), tmp_path) as sequence:
        for clip in clips:
            sequence.append(clip)
        sequence.seal()
        assert fit_centerline_variants(replace(result, clips=sequence)) == fit_centerline_variants(result)


def test_pair_sampling_rejects_changed_count_and_uses_multiple_blocks(monkeypatch):
    import track.v2.fitting as fitting
    clips = [_clip(str(i), 1, 2, [(0, i), (40, i + 1), (200, i)]) for i in range(3)]
    oriented = [_orient_clip(clip, _TRANSFORMER)[0] for clip in clips]
    calls = 0

    def read_clips():
        nonlocal calls
        calls += 1
        return iter(oriented)

    monkeypatch.setattr(fitting, "_FIT_BLOCK_BYTES", 48)
    assert _fit_pair_stream(
        (1, 2), read_clips, 3, FittingConfig(fit_sample_spacing_m=5), _TRANSFORMER,
    )
    assert calls > 2
    with pytest.raises(ValueError, match="count changed"):
        _fit_pair_stream((1, 2), lambda: iter(oriented[:2]), 3, FittingConfig(), _TRANSFORMER)


def test_clip_and_fitting_do_not_use_legacy_spill_implementations():
    import track.v2.clip as clip_module
    import track.v2.fitting as fitting_module
    assert "DiskSequence" not in inspect.getsource(clip_module)
    fitting_source = inspect.getsource(fitting_module)
    for forbidden in ("DiskSequence", "TemporaryDirectory", "np.memmap", "import mmap"):
        assert forbidden not in fitting_source


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.consumed = 0

    def execute(self, query, params):
        self.query, self.params = query, params

    def __iter__(self):
        for row in self.rows:
            self.consumed += 1
            yield row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


class ControlCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        if query.startswith("SET LOCAL cursor_tuple_fraction"):
            self.conn.fraction = "1"
        elif "set_config" in query:
            self.conn.fraction = params[0]

    def fetchone(self):
        return (self.conn.fraction,)

    def __iter__(self):
        return iter([(10, 10)])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Connection:
    def __init__(self, rows):
        self.value = Cursor(rows)
        self.fraction = "0.37"

    def cursor(self, *, name=None):
        if name is None:
            return ControlCursor(self)
        assert name.startswith("clip_tracks_")
        return self.value

    @contextmanager
    def transaction(self):
        previous = self.fraction
        try:
            yield
        except BaseException:
            self.fraction = previous
            raise


def _reader(conn):
    return fetch_resolved_clip_tracks(conn, "d")


def test_resolved_reader_streams_subtracks_inherits_leading_crossing_vehicle_and_closes():
    # Crossing at position zero must not require a vehicle on that first row.
    rows = [(1, 1, 0, None, 1., 2., 11, 10, 0),
            (2, 1, 1, "v", 1., 2., None, None, None),
            (3, 2, 0, "w", 1., 2., None, None, None),
            (4, 2, 1, "w", 1., 2., None, None, None)]
    conn = Connection(rows)
    with (
        patch("track.v2.db.queries.fetch_detect_row", return_value=SimpleNamespace(status="completed", source_table="s")),
        patch("track.v2.db.queries.validate_source_table", return_value=SimpleNamespace(quoted_table='"s"', key_sql_type="bigint")),
    ):
        stream = _reader(conn)
        first = next(stream)
        assert conn.fraction == "0.37"
        assert first.vehicle_id == "v"
        assert [point.resolved_point_id for point in first.points] == [1, 2]
        assert conn.value.consumed == 3
        stream.close()
    assert conn.value.closed
    assert conn.fraction == "0.37"


def test_resolved_reader_rejects_mixed_vehicles_instead_of_dropping_track():
    conn = Connection([(1, 1, 0, "v", 1., 2., None, None, None),
                       (2, 1, 1, "w", 1., 2., None, None, None)])
    with (
        patch("track.v2.db.queries.fetch_detect_row", return_value=SimpleNamespace(status="completed", source_table="s")),
        patch("track.v2.db.queries.validate_source_table", return_value=SimpleNamespace(quoted_table='"s"', key_sql_type="bigint")),
        pytest.raises(ValueError, match="multiple vehicles"),
    ):
        list(_reader(conn))
    assert conn.value.closed

def _reference_fit_pair(
    pair: tuple[int, int],
    clips: list[_OrientedClip],
    config: FittingConfig,
    transformer: CoordinateTransformer,
) -> Centerline | None:
    tracks_xy = [np.asarray(clip.points_xy) for clip in clips]
    lengths = [_xy_polyline_length(track) for track in tracks_xy]
    median_length = float(np.median(lengths))
    if median_length <= _EPS:
        return None

    sample_count = max(2, int(math.ceil(median_length / config.fit_sample_spacing_m)) + 1)
    progress = np.linspace(0.0, 1.0, sample_count)
    sampled = np.stack([_resample_at_progress(track, progress) for track in tracks_xy])
    fitted_xy = np.median(sampled, axis=0)
    if _xy_polyline_length(fitted_xy) <= _EPS:
        return None
    spaced_xy = _resample_at_spacing(fitted_xy, config.point_spacing_m)
    if len(spaced_xy) < 2:
        return None
    points = tuple(
        transformer.inverse(float(x), float(y))
        for x, y in spaced_xy
    )
    return Centerline(
        from_intersection_id=pair[0],
        to_intersection_id=pair[1],
        points=points,
        length_m=_xy_polyline_length(spaced_xy),
        source_clip_ids=tuple(clip.value.clip_id for clip in clips),
        forward_clip_count=sum(not clip.was_reversed for clip in clips),
        reverse_clip_count=sum(clip.was_reversed for clip in clips),
    )




def test_fitting_heartbeat_counts_have_separate_scopes_and_units():
    from track.v2 import heartbeat

    clips = _clip_result(
        _clip("forward", 1, 2, [(0, 0), (100, 10), (200, 0)]),
        _clip("reverse", 2, 1, [(200, 0), (100, -10), (0, 0)]),
        replace(_clip("boundary", 1, 2, [(0, 0), (100, 0)]), clip_type="full_track"),
    )
    statuses = []
    original = heartbeat.heartbeat_progress

    def capture(completed, *, total=None, unit="行"):
        original(completed, total=total, unit=unit)
        statuses.append(heartbeat._current.get().status)

    with patch("track.v2.fitting.heartbeat_progress", side_effect=capture):
        fit_centerline_variants(clips)

    scopes = {}
    for status in statuses:
        scopes.setdefault((status.operation, status.started), []).append(status)
    names = {name for name, _started in scopes}
    assert names == {
        "Fitting：准备片段", "Fitting：按交叉口对拟合",
        "Fitting：筛选正向片段", "Fitting：筛选反向片段",
    }
    for (name, _started), samples in scopes.items():
        expected_unit = "交叉口对" if name == "Fitting：按交叉口对拟合" else "片段"
        assert {sample.unit for sample in samples} == {expected_unit}
        assert {sample.total for sample in samples} == {samples[-1].total}
        assert [sample.completed for sample in samples] == list(range(samples[-1].total + 1))


def test_pair_sampling_does_not_overwrite_outer_heartbeat_counts():
    from track.v2 import heartbeat

    clip = _clip("single", 1, 2, [(0, 0), (100, 10), (200, 0)])
    oriented = _orient_clip(clip, _TRANSFORMER)[0]
    with heartbeat.operation_heartbeat("outer fitting"):
        heartbeat.heartbeat_progress(7, total=10, unit="交叉口对")
        before = heartbeat._current.get().status
        assert _fit_pair_stream((1, 2), lambda: iter([oriented]), 1, FittingConfig(), _TRANSFORMER)
        assert heartbeat._current.get().status == before
