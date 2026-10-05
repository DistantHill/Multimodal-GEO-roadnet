"""Reject invalid field definitions before any source rows are consumed."""

from pathlib import Path

import pytest

from track.v2.config.field_mapping import load_field_config
from track.v2.errors import ConfigError


def _write_config(tmp_path: Path, *, unit_scale: str = "1.0", field_type: str = '"float"') -> Path:
    path = tmp_path / "fields.toml"
    path.write_text(
        f"""[quality]
sample_errors_max = 50
max_consecutive_errors = 50
max_error_ratio = 1.0

[[field]]
canonical = "lon"
aliases = ["lng"]
required = true
type = {field_type}
unit_scale = {unit_scale}
has_default = false
extra = false
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "unit_scale",
    ["nan", "+nan", "-nan", "inf", "+inf", "-inf", "0", "-1", "true", '"1"', "1" + "0" * 400],
)
def test_invalid_unit_scale_is_rejected(tmp_path: Path, unit_scale: str) -> None:
    path = _write_config(tmp_path, unit_scale=unit_scale)
    with pytest.raises(ConfigError, match=r"field\[0\]\.unit_scale: must be a finite number > 0"):
        load_field_config(path)


@pytest.mark.parametrize("field_type", ["[]", "{}", "1", "true", '"unsupported"'])
def test_invalid_field_type_reports_config_error(tmp_path: Path, field_type: str) -> None:
    path = _write_config(tmp_path, field_type=field_type)
    with pytest.raises(ConfigError, match=r"field\[0\]\.type: unsupported field type"):
        load_field_config(path)


def test_field_errors_remain_aggregated(tmp_path: Path) -> None:
    path = _write_config(tmp_path, unit_scale="inf", field_type="[]")
    with pytest.raises(ConfigError) as raised:
        load_field_config(path)
    assert "field[0].unit_scale" in str(raised.value)
    assert "field[0].type" in str(raised.value)


@pytest.mark.parametrize("unit_scale, expected", [("1", 1.0), ("0.001", 0.001)])
def test_positive_finite_unit_scale_is_preserved(tmp_path: Path, unit_scale: str, expected: float) -> None:
    config = load_field_config(_write_config(tmp_path, unit_scale=unit_scale))
    assert config.specs[0].unit_scale == expected
