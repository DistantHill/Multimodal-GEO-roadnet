"""Readable experiment drafts preserve TOML values and align descriptions."""
import tomllib

from track.v2.cli.experiment import toml_text


def test_bare_keys_and_quoted_special_names_round_trip():
    payload = {"cases": {"区域.一": {"source_table": "trajectory_points", "time_start": ""}},
               "schemes": {"turn": {"detect": {"use_turn_events": True}}}}
    output = toml_text(payload)
    assert '[cases."区域.一"]' in output
    assert '[schemes.turn.detect]' in output
    assert '[schemes]\n' not in output
    assert 'source_table = "trajectory_points"' in output
    assert tomllib.loads(output) == payload
    assert all(line.index('#') == 69 for line in output.splitlines() if '#' in line)


def test_long_parameter_moves_comment_to_next_line():
    payload = {"cases": {"example": {"region_file": "a" * 120 + ".geojson"}}}
    output = toml_text(payload)
    lines = output.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('region_file'))
    assert '#' not in lines[index]
    assert lines[index + 1].startswith(' ' * 69 + '# ')
    assert tomllib.loads(output) == payload


def test_empty_region_placeholder_explains_bbox_alternative():
    payload = {"cases": {"example": {"region_file": ""}}}
    output = toml_text(payload)
    assert 'region_file = ""' in output
    assert '# bbox = [[经度1, 纬度1], [经度2, 纬度2]]' in output
    assert tomllib.loads(output) == payload
    assert all(line.index('#') == 69 for line in output.splitlines() if '#' in line)
