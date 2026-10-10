"""Geometry reuse preserves the independent eager numerical/reference results."""
from collections import Counter
from dataclasses import replace
import gc
import sys
import weakref

import numpy as np
import pytest

import track.v2.centerlines.fit_centerlines as fitting
from track.v2.models import CenterlineFitBundle, FitIssue, FitReport, FitResult
from .config_factory import fitting_config
from .test_clip_relation import _relation
from .test_clip_fitting_streaming import _reference_fit_pair
from .test_fitting_network import _clip, _clip_result, _TRANSFORMER


def _eager_reference(result, config, reverse=None):
    """Retain pre-reuse traversal semantics, use the full-matrix median oracle."""
    selected = [c for c in result.clips if reverse is None or (
        c.clip_type == "between_intersection" and c.start_intersection_id is not None
        and c.end_intersection_id is not None and c.start_intersection_id != c.end_intersection_id
        and (c.start_intersection_id > c.end_intersection_id) == reverse
    )]
    groups = {}
    issues = []
    eligible = 0
    for clip in selected:
        if clip.clip_type in {"boundary_start", "boundary_end", "full_track"}:
            continue
        if clip.clip_type != "between_intersection":
            issues.append(FitIssue(None, None, "unsupported_clip_type", clip.clip_id))
            continue
        eligible += 1
        start, end = clip.start_intersection_id, clip.end_intersection_id
        if start is not None and end is not None and start != end:
            pair = (min(start, end), max(start, end))
            groups.setdefault(pair, [])
        oriented, issue = fitting._orient_clip(clip, _TRANSFORMER)
        if issue:
            issues.append(issue)
        else:
            groups[pair].append(oriented)
    centerlines = []
    skipped = 0
    for pair, values in sorted(groups.items()):
        values.sort(key=lambda c: c.value.clip_id)
        if len(values) < config.min_clips_per_pair:
            issues.append(FitIssue(*pair, "insufficient_clips"))
            skipped += 1
            continue
        line = _reference_fit_pair(pair, values, config, _TRANSFORMER)
        if line is None:
            issues.append(FitIssue(*pair, "degenerate_centerline"))
            skipped += 1
            continue
        if reverse is not None:
            line = replace(line, variant="directed", points=line.points[::-1] if reverse else line.points,
                           from_intersection_id=pair[1] if reverse else pair[0],
                           to_intersection_id=pair[0] if reverse else pair[1],
                           forward_clip_count=line.reverse_clip_count if reverse else line.forward_clip_count,
                           reverse_clip_count=line.forward_clip_count if reverse else line.reverse_clip_count)
        centerlines.append(line)
    return FitResult(centerlines=tuple(centerlines), issues=tuple(issues),
                     report=FitReport(input_clip_count=len(selected), eligible_clip_count=eligible,
                                      connected_pair_count=len(groups), fitted_centerline_count=len(centerlines),
                                      skipped_clip_count=sum(i.clip_id is not None for i in issues),
                                      skipped_pair_count=skipped),
                     spatial_context=result.spatial_context, detect_id=result.detect_id)


def _reference_bundle(result, config):
    return CenterlineFitBundle(combined=_eager_reference(result, config),
                               a_to_b=_eager_reference(result, config, False),
                               b_to_a=_eager_reference(result, config, True))


def _valid_clips():
    # Different lengths/curvature give each direction a different sampling grid.
    # distance_m stays 200 even though these geometries have other lengths.
    return (
        _clip("z-forward", 1, 2, [(0, 0), (10, 35), (125, 0)]),
        _clip("a-reverse", 2, 1, [(220, 4), (100, -20), (0, 4)]),
        _clip("b-forward", 1, 2, [(0, 8), (0, 8), (1e-10, 8), (65, 25), (125, 8)]),
        _clip("other", 3, 4, [(300, 0), (375, 17), (425, 0)]),
        _clip("zero-reverse", 6, 5, [(20, 1), (20, 1)]),
        _clip("zero-forward", 5, 6, [(20, 2), (20, 2)]),
        replace(_clip("boundary", 1, 2, [(0, 0), (125, 0)]),
                clip_type="full_track", start_kind="boundary", end_kind="boundary",
                start_intersection_id=None, end_intersection_id=None,
                start_crossing_id=None, end_crossing_id=None),
        _clip("same", 7, 7, [(0, 0), (125, 0)]),
    )


@pytest.mark.parametrize("minimum", [1, 2, 3])
@pytest.mark.parametrize("storage", ["tuple", "duckdb"])
def test_shared_preparation_is_exact_for_all_variant_fields_and_reports(tmp_path, minimum, storage):
    result = _clip_result(*_valid_clips())
    config = fitting_config(min_clips_per_pair=minimum, fit_sample_spacing_m=7, point_spacing_m=19)
    expected = _reference_bundle(result, config).as_dict()
    if storage == "tuple":
        actual = fitting.fit_centerline_variants(result, config=config)
        assert actual.as_dict() == expected
        assert fitting.fit_centerlines(result, config=config).as_dict() == actual.combined.as_dict()
    else:
        with _relation(tmp_path, batch_size=2) as relation:
            for clip in result.clips:
                relation.append(clip)
            relation.seal()
            actual = fitting.fit_centerline_variants(replace(result, clips=relation), config=config)
            assert actual.as_dict() == expected
            assert len(relation) == len(result.clips)
            assert [c.clip_id for c in relation] == [c.clip_id for c in result.clips]
    for clip in result.clips:
        assert not clip.xy.flags.writeable
        assert not clip.lonlat.flags.writeable


def test_metadata_and_geometry_issues_keep_input_order_and_direction_eligibility():
    clips = (
        _clip("late-pair-zero", 9, 10, [(0, 0), (0, 0)]),
        replace(_clip("unknown", 1, 2, [(0, 0), (100, 0)]), clip_type="typo"),
        _clip("early-pair-invalid", 2, 1, [(100, 0), (float("nan"), 0)]),
        replace(_clip("missing", 1, 2, [(0, 0), (100, 0)]), start_intersection_id=None),
        _clip("same", 7, 7, [(0, 0), (100, 0)]),
        _clip("early-pair-zero", 1, 2, [(0, 0), (0, 0)]),
    )
    result = _clip_result(*clips)
    config = fitting_config()
    actual = fitting.fit_centerline_variants(result, config=config)
    assert actual.as_dict() == _reference_bundle(result, config).as_dict()
    assert [i.clip_id for i in actual.combined.issues] == [
        "late-pair-zero", "unknown", "early-pair-invalid", "missing", "same",
        "early-pair-zero", None, None,
    ]
    assert [i.clip_id for i in actual.a_to_b.issues if i.clip_id] == ["late-pair-zero", "early-pair-zero"]
    assert [i.clip_id for i in actual.b_to_a.issues if i.clip_id] == ["early-pair-invalid"]


def test_cache_hit_decodes_only_candidate_geometry_once(tmp_path, monkeypatch):
    result = _clip_result(*_valid_clips())
    decoded = Counter()
    with _relation(tmp_path) as relation:
        for clip in result.clips:
            relation.append(clip)
        relation.seal()
        original = relation._decode

        def decode(row, values):
            decoded[row[1]] += 1
            return original(row, values)

        monkeypatch.setattr(relation, "_decode", decode)
        actual = fitting.fit_centerline_variants(replace(result, clips=relation), config=fitting_config())
        assert actual.as_dict() == _reference_bundle(result, fitting_config()).as_dict()
        assert decoded == Counter({c.clip_id: 1 for c in result.clips
                                   if c.clip_type == "between_intersection"
                                   and c.start_intersection_id != c.end_intersection_id})


@pytest.mark.parametrize("budget,first_large", [(400, True), (1800, False), (8, False)])
def test_cache_overflow_falls_back_exactly_and_releases_arrays(tmp_path, monkeypatch, budget, first_large):
    clips = tuple(_clip(str(i), 1 if i % 2 == 0 else 2, 2 if i % 2 == 0 else 1,
                        [(float(x), i + x % 3) for x in range(0, 101, 2)]
                        if first_large and i == 0 else [(0, i), (50, i + 20), (100, i)])
                  for i in range(5))
    # Preserve physical direction when coordinates are reversed.
    clips = tuple(replace(c, xy=c.xy[::-1], lonlat=c.lonlat[::-1]) if i % 2 else c
                  for i, c in enumerate(clips))
    result = _clip_result(*clips)
    config = fitting_config(fit_sample_spacing_m=4, point_spacing_m=13)
    expected = _reference_bundle(result, config).as_dict()
    refs = []
    prepared = []
    original_geometry = fitting._prepare_geometry
    original_pair = fitting._prepare_pair
    original_empty = np.empty
    matrix_sizes = []

    def geometry(points):
        values = original_geometry(points)
        refs.extend(weakref.ref(array) for array in values)
        return values

    def pair(*args):
        before = len(refs)
        statistics, cache = original_pair(*args)
        prepared.append((cache is None, (len(refs) - before) // 2))
        return statistics, cache

    def empty(shape, *args, **kwargs):
        array = original_empty(shape, *args, **kwargs)
        if isinstance(shape, tuple) and len(shape) == 3:
            matrix_sizes.append((shape, array.nbytes))
        return array

    monkeypatch.setattr(fitting, "_FIT_BLOCK_BYTES", budget)
    monkeypatch.setattr(fitting, "_prepare_geometry", geometry)
    monkeypatch.setattr(fitting, "_prepare_pair", pair)
    monkeypatch.setattr(fitting.np, "empty", empty)
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        relation.seal()
        decoded = Counter()
        original_decode = relation._decode

        def decode(row, values):
            decoded[row[1]] += 1
            return original_decode(row, values)

        monkeypatch.setattr(relation, "_decode", decode)
        assert fitting.fit_centerline_variants(replace(result, clips=relation), config=config).as_dict() == expected
        assert prepared[0][0]
        if first_large or budget == 8:
            assert prepared[0][1] == 1
        else:
            assert prepared[0][1] > 1
        assert all(decoded[c.clip_id] > 1 for c in clips)
        gc.collect()
        assert all(ref() is None for ref in refs)
        assert len(relation) == len(clips)
        if budget == 8:
            assert matrix_sizes
            assert all(shape[1] == 1 and size > budget for shape, size in matrix_sizes)


def test_cache_and_multiblock_sampling_share_actual_workspace_budget(monkeypatch):
    clips = tuple(_clip(str(i), 1, 2, [(0, i), (60, i * i), (200, -i)]) for i in range(5))
    result = _clip_result(*clips)
    config = fitting_config(fit_sample_spacing_m=1)
    expected = _reference_bundle(result, config).as_dict()
    budget = 4096
    monkeypatch.setattr(fitting, "_FIT_BLOCK_BYTES", budget)
    original_fit = fitting._fit_pair_stream
    original_empty = np.empty
    matrix_sizes = []
    retained = 0

    def fit(*args, **kwargs):
        nonlocal retained
        cache = kwargs["cache"]
        assert cache is not None
        # Independently count retained NumPy allocations and their object/container overhead.
        arrays = [a for item in cache for a in (item.points_xy, item.normalized)]
        assert all(a.flags.owndata for a in arrays)
        data_bytes = sum(a.nbytes for a in arrays)
        retained = sys.getsizeof(cache) + sum(sys.getsizeof(c) for c in cache) + sum(sys.getsizeof(a) for a in arrays)
        assert data_bytes < retained <= budget // 2
        assert fitting._geometry_cache_bytes(cache) == retained
        return original_fit(*args, **kwargs)

    def empty(shape, *args, **kwargs):
        array = original_empty(shape, *args, **kwargs)
        if isinstance(shape, tuple) and len(shape) == 3:
            matrix_sizes.append(array.nbytes)
            assert retained + array.nbytes <= budget
        return array

    monkeypatch.setattr(fitting, "_fit_pair_stream", fit)
    monkeypatch.setattr(fitting.np, "empty", empty)
    assert fitting.fit_centerline_variants(result, config=config).as_dict() == expected
    assert len(matrix_sizes) > 2


@pytest.mark.parametrize("failure", ["prepare", "sample"])
def test_cache_exception_releases_prepared_arrays_and_preserves_relation(tmp_path, monkeypatch, failure):
    result = _clip_result(*_valid_clips()[:3])
    refs = []
    original_geometry = fitting._prepare_geometry
    original_median = np.median
    calls = 0

    def geometry(points):
        nonlocal calls
        calls += 1
        if failure == "prepare" and calls == 2:
            raise RuntimeError("preparation failed")
        values = original_geometry(points)
        refs.extend(weakref.ref(array) for array in values)
        return values

    def median(array, *args, **kwargs):
        if failure == "sample" and array.ndim == 3:
            raise RuntimeError("sampling failed")
        return original_median(array, *args, **kwargs)

    monkeypatch.setattr(fitting, "_prepare_geometry", geometry)
    monkeypatch.setattr(fitting.np, "median", median)
    with _relation(tmp_path) as relation:
        for clip in result.clips:
            relation.append(clip)
        relation.seal()
        with pytest.raises(RuntimeError, match="failed") as error:
            fitting.fit_centerline_variants(replace(result, clips=relation), config=fitting_config())
        # Keep the exception traceback alive: owned cache arrays still must be released.
        assert error.value is not None
        gc.collect()
        assert refs and all(ref() is None for ref in refs)
        assert [i for i, _ in relation.iter_fitting_pair((1, 2))] == [1, 2, 0]


def test_empty_geometry_and_empty_input_preserve_reports():
    base = _clip("empty", 1, 2, [(0, 0), (100, 0)])
    empty = replace(base, xy=np.empty((0, 2)), lonlat=np.empty((0, 2)),
                    resolved_point_ids=np.empty(0, dtype=np.int64), point_count=0)
    for result in (_clip_result(), _clip_result(empty)):
        assert fitting.fit_centerline_variants(result, config=fitting_config()).as_dict() == _reference_bundle(result, fitting_config()).as_dict()


@pytest.mark.parametrize("storage", ["tuple", "duckdb"])
def test_short_segments_keep_original_length_and_fitting_threshold_errors(tmp_path, storage):
    # Original length exceeds EPS, but each segment is discarded by deduplication.
    # The old sampler raises only when the pair meets the fitting threshold.
    result = _clip_result(_clip("tiny", 1, 2, [(0, 0), (6e-10, 0), (1.2e-9, 0)]))

    def check(value):
        config = fitting_config(min_clips_per_pair=2)
        assert fitting.fit_centerline_variants(value, config=config).as_dict() == _reference_bundle(result, config).as_dict()
        with pytest.raises(ValueError, match="at least two distinct points"):
            fitting.fit_centerline_variants(value, config=fitting_config())

    if storage == "tuple":
        check(result)
    else:
        with _relation(tmp_path) as relation:
            relation.append(result.clips[0])
            relation.seal()
            check(replace(result, clips=relation))
