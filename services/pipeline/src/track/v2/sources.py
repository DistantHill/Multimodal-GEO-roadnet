from __future__ import annotations

import csv
import math

# ZH: 提高 CSV 字段上限至 10 MB（默认 131 KB），防止合法大字段触发 _csv.Error，
#     同时避免超大值在遇到损坏行（引号不配对）时导致无限 buffering 和 PermissionError。
# EN: Raise CSV field size limit to 10 MB (default 131 KB) to allow legit large fields
#     while preventing infinite buffering / PermissionError on malformed rows (unbalanced quotes).
_MAX_FIELD_SIZE = 10 * 1024 * 1024  # 10 MB
if _MAX_FIELD_SIZE > csv.field_size_limit():
    csv.field_size_limit(_MAX_FIELD_SIZE)
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .errors import (
    DataSourceCorrupted,
    EncodingDetectionError,
    ResourceLeakWarning,
    TrajectoryOrderError,
    V2Error,
)
from .field_config import build_field_mapping, load_field_config
from .models import (
    ColumnSelector,
    CsvSource,
    FieldConfig,
    FieldMapping,
    FieldSpec,
    ParseIssue,
    PointRef,
    PostgresSource,
    ReadReport,
    SourceInfo,
    TrajectoryPoint,
)


DEFAULT_ENCODING_SAMPLE_SIZE = 64 * 1024
DEFAULT_ENCODING_CONFIDENCE = 0.5
DEFAULT_TEXT_ENCODING = "utf-8-sig"

_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)
_FALLBACK_ENCODINGS = ("gb18030", "gbk", "cp936", "big5")


def get_database(
    source: CsvSource | PostgresSource,
    *,
    field_config: str | Path | None = None,
    selector: ColumnSelector | None = None,
) -> "BaseTrajectorySourceHandle":
    config = load_field_config(field_config)
    if isinstance(source, CsvSource):
        return CsvTrajectorySourceHandle(source, config, selector)
    if isinstance(source, PostgresSource):
        return PostgresTrajectorySourceHandle(source, config, selector)
    raise TypeError(f"unsupported source type: {type(source).__name__}")


@dataclass
class _ReadState:
    source_info: SourceInfo
    sample_errors_max: int
    max_consecutive_errors: int
    max_error_ratio: float
    total_rows: int = 0
    loaded_rows: int = 0
    skipped_rows: int = 0
    warn_count: int = 0
    error_count: int = 0
    fatal_count: int = 0
    consecutive_errors: int = 0
    sample_errors: list[ParseIssue] | None = None

    def __post_init__(self) -> None:
        if self.sample_errors is None:
            self.sample_errors = []

    def report(self) -> ReadReport:
        return ReadReport(
            source_info=self.source_info,
            total_rows=self.total_rows,
            loaded_rows=self.loaded_rows,
            skipped_rows=self.skipped_rows,
            warn_count=self.warn_count,
            error_count=self.error_count,
            fatal_count=self.fatal_count,
            sample_errors=list(self.sample_errors or []),
            sample_errors_max=self.sample_errors_max,
        )

    def add_issue(self, issue: ParseIssue) -> None:
        if issue.level == "WARN":
            self.warn_count += 1
        elif issue.level == "ERROR":
            self.error_count += 1
            self.skipped_rows += 1
            self.consecutive_errors += 1
        elif issue.level == "FATAL":
            self.fatal_count += 1

        if self.sample_errors is not None and len(self.sample_errors) < self.sample_errors_max:
            self.sample_errors.append(issue)

    def mark_loaded(self) -> None:
        self.loaded_rows += 1
        self.consecutive_errors = 0

    def assert_thresholds(self) -> None:
        if self.consecutive_errors > self.max_consecutive_errors:
            raise DataSourceCorrupted(
                "consecutive bad rows exceeded "
                f"{self.max_consecutive_errors}: {self.consecutive_errors}"
            )
        if self.total_rows and (self.skipped_rows / self.total_rows) > self.max_error_ratio:
            raise DataSourceCorrupted(
                "bad row ratio exceeded "
                f"{self.max_error_ratio}: {self.skipped_rows}/{self.total_rows}"
            )


class BaseTrajectorySourceHandle:
    def __init__(self, source_info: SourceInfo, config: FieldConfig, mapping: FieldMapping) -> None:
        self._source_info = source_info
        self._config = config
        self._mapping = mapping
        self._state = self._new_state()
        self._closed = False

    @property
    def source_info(self) -> SourceInfo:
        return self._source_info

    @property
    def field_mapping(self) -> FieldMapping:
        return self._mapping

    @property
    def report(self) -> ReadReport:
        return self._state.report()

    @property
    def read_report(self) -> ReadReport:
        return self.report

    def get_report(self) -> ReadReport:
        return self.report

    def iter_points(self) -> Iterator[TrajectoryPoint]:
        raise NotImplementedError

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "BaseTrajectorySourceHandle":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            warnings.warn(
                f"{type(self).__name__} was garbage-collected without close()",
                ResourceLeakWarning,
                stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass

    def _new_state(self) -> _ReadState:
        return _ReadState(
            source_info=self._source_info,
            sample_errors_max=self._config.sample_errors_max,
            max_consecutive_errors=self._config.max_consecutive_errors,
            max_error_ratio=self._config.max_error_ratio,
        )


class CsvTrajectorySourceHandle(BaseTrajectorySourceHandle):
    def __init__(
        self,
        source: CsvSource,
        config: FieldConfig,
        selector: ColumnSelector | None,
    ) -> None:
        self._source = source
        self._path = Path(source.path)
        self._encoding = detect_encoding(self._path, hint_encoding=source.encoding)
        columns = _read_csv_columns(self._path, self._encoding)
        source_info = SourceInfo(
            kind="csv",
            path_or_dsn=str(self._path.resolve()),
            encoding=self._encoding,
            column_count=len(columns),
            columns=columns,
        )
        mapping = build_field_mapping(config.specs, columns, selector)
        super().__init__(source_info, config, mapping)

    def iter_points(self) -> Iterator[TrajectoryPoint]:
        self._state = self._new_state()
        previous: TrajectoryPoint | None = None
        with open_text_auto(self._path, encoding=self._encoding, newline="") as f:
            reader = csv.DictReader(f)
            for row_number, row in enumerate(reader, start=2):
                self._state.total_rows += 1
                point_ref = PointRef(str(self._path), str(row_number), True)
                try:
                    point, issues = parse_row(row, point_ref, row_number, self._mapping)
                except _SkipRow as exc:
                    self._state.add_issue(exc.issue)
                    self._state.assert_thresholds()
                    continue

                for issue in issues:
                    self._state.add_issue(issue)
                _check_order(previous, point)
                previous = point
                self._state.mark_loaded()
                yield point
                self._state.assert_thresholds()
        self._state.assert_thresholds()


class PostgresTrajectorySourceHandle(BaseTrajectorySourceHandle):
    def __init__(
        self,
        source: PostgresSource,
        config: FieldConfig,
        selector: ColumnSelector | None,
    ) -> None:
        self._source = source
        columns = self._discover_columns()
        source_info = SourceInfo(
            kind="postgres",
            path_or_dsn=self._source_id(),
            encoding=None,
            column_count=len(columns),
            columns=columns,
        )
        mapping = build_field_mapping(config.specs, columns, selector)
        super().__init__(source_info, config, mapping)

    def iter_points(self) -> Iterator[TrajectoryPoint]:
        self._state = self._new_state()
        previous: TrajectoryPoint | None = None
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            cur.execute(self._source.query, self._source.params)
            columns = [desc[0] for desc in (cur.description or [])]
            for ordinal, row in enumerate(cur, start=1):
                self._state.total_rows += 1
                row_dict = _row_to_dict(row, columns)
                point_ref = self._point_ref(row_dict, ordinal)
                try:
                    point, issues = parse_row(row_dict, point_ref, ordinal, self._mapping)
                except _SkipRow as exc:
                    self._state.add_issue(exc.issue)
                    self._state.assert_thresholds()
                    continue

                for issue in issues:
                    self._state.add_issue(issue)
                _check_order(previous, point)
                previous = point
                self._state.mark_loaded()
                yield point
                self._state.assert_thresholds()
            self._state.assert_thresholds()
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    def _discover_columns(self) -> list[str]:
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            cur.execute(self._source.query, self._source.params)
            return [desc[0] for desc in (cur.description or [])]
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    def _point_ref(self, row: dict[str, Any], ordinal: int) -> PointRef:
        key = self._source.primary_key_field
        if key in row and row[key] not in (None, ""):
            return PointRef(self._source_id(), str(row[key]), True)
        return PointRef(self._source_id(), str(ordinal), False)

    def _source_id(self) -> str:
        if self._source.source_id is not None:
            return self._source.source_id
        safe_dsn = getattr(self._source.config, "safe_dsn", None)
        if callable(safe_dsn):
            return str(safe_dsn())
        return str(self._source.config)

    def _connect(self) -> Any:
        if self._source.connection_factory is not None:
            return self._source.connection_factory(self._source.config)
        try:
            import psycopg  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise V2Error("PostgresSource requires psycopg or connection_factory") from exc
        dsn = self._source.config.dsn() if hasattr(self._source.config, "dsn") else str(self._source.config)
        return psycopg.connect(dsn)


class _SkipRow(Exception):
    def __init__(self, issue: ParseIssue) -> None:
        self.issue = issue
        super().__init__(issue.reason)


def parse_row(
    row: dict[str, Any],
    point_ref: PointRef,
    row_number: int,
    mapping: FieldMapping,
) -> tuple[TrajectoryPoint, list[ParseIssue]]:
    specs = {spec.canonical: spec for spec in mapping.specs}
    issues: list[ParseIssue] = []

    vehicle_id = _required_str(row, specs["vehicle_id"], mapping, point_ref, row_number)
    timestamp = _required_timestamp(row, specs["timestamp"], mapping, point_ref, row_number)
    lon = _required_float(row, specs["lon"], mapping, point_ref, row_number)
    lat = _required_float(row, specs["lat"], mapping, point_ref, row_number)
    if not -180.0 <= lon <= 180.0:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "lon out of bounds", row))
    if not -90.0 <= lat <= 90.0:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "lat out of bounds", row))

    heading = _optional_float(row, specs.get("heading"), mapping, point_ref, row_number, issues)
    if not math.isfinite(heading):
        heading = _float_default(specs.get("heading"))
        issues.append(_issue(point_ref, row_number, "WARN", "heading non-finite; using default", row))
    elif not 0.0 <= heading < 360.0:
        heading %= 360.0
        issues.append(_issue(point_ref, row_number, "WARN", "heading normalized to [0, 360)", row))

    speed = _optional_float(row, specs.get("speed"), mapping, point_ref, row_number, issues)
    if not math.isfinite(speed) or speed < 0.0:
        speed = _float_default(specs.get("speed"))
        issues.append(_issue(point_ref, row_number, "WARN", "speed invalid; using default", row))

    extra = _parse_extra(row, mapping, point_ref, row_number, issues)
    return (
        TrajectoryPoint(
            point_ref=point_ref,
            vehicle_id=vehicle_id,
            timestamp=timestamp,
            lon=lon,
            lat=lat,
            heading=heading,
            speed=speed,
            extra=extra,
        ),
        issues,
    )


def detect_encoding(
    file_path: str | Path,
    *,
    hint_encoding: str | None = None,
    sample_size: int = DEFAULT_ENCODING_SAMPLE_SIZE,
    min_confidence: float = DEFAULT_ENCODING_CONFIDENCE,
) -> str:
    path = Path(file_path)
    with path.open("rb") as f:
        raw = f.read(sample_size)
    if not raw:
        return hint_encoding or DEFAULT_TEXT_ENCODING

    for prefix, encoding in _BOM_ENCODINGS:
        if raw.startswith(prefix):
            return encoding

    if hint_encoding is not None and _can_decode(raw, hint_encoding):
        return hint_encoding
    if _can_decode(raw, DEFAULT_TEXT_ENCODING):
        return DEFAULT_TEXT_ENCODING

    guessed, confidence = _guess_with_chardet(raw)
    if guessed is not None and confidence >= min_confidence:
        encoding = _canonical_encoding(guessed)
        if _can_decode(raw, encoding):
            return encoding

    for encoding in _FALLBACK_ENCODINGS:
        if _can_decode(raw, encoding):
            return encoding

    raise EncodingDetectionError(f"unable to detect text encoding for {path}")


def open_text_auto(
    file_path: str | Path,
    *,
    encoding: str | None = None,
    newline: str | None = None,
    errors: str = "strict",
) -> TextIO:
    path = Path(file_path)
    return path.open("r", encoding=encoding or detect_encoding(path), newline=newline, errors=errors)


def _required_str(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: PointRef,
    row_number: int,
) -> str:
    raw = _raw_value(row, spec, mapping)
    value = "" if raw is None else str(raw).strip()
    if not value:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} missing", row))
    return value


def _required_timestamp(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: PointRef,
    row_number: int,
) -> float:
    raw = _raw_value(row, spec, mapping)
    try:
        value = _parse_timestamp(raw) * spec.unit_scale
    except (TypeError, ValueError, OverflowError) as exc:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "timestamp invalid", row)) from exc
    if not math.isfinite(value):
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "timestamp non-finite", row))
    return value


def _required_float(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: PointRef,
    row_number: int,
) -> float:
    raw = _raw_value(row, spec, mapping)
    try:
        value = float(raw) * spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} invalid", row)) from exc
    if not math.isfinite(value):
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} non-finite", row))
    return value


def _optional_float(
    row: dict[str, Any],
    spec: FieldSpec | None,
    mapping: FieldMapping,
    point_ref: PointRef,
    row_number: int,
    issues: list[ParseIssue],
) -> float:
    if spec is None:
        return 0.0
    raw = _raw_value(row, spec, mapping)
    if raw in (None, ""):
        if spec.required:
            issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} missing; using default", row))
        return _float_default(spec)
    try:
        return float(raw) * spec.unit_scale
    except (TypeError, ValueError):
        issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} invalid; using default", row))
        return _float_default(spec)


def _parse_extra(
    row: dict[str, Any],
    mapping: FieldMapping,
    point_ref: PointRef,
    row_number: int,
    issues: list[ParseIssue],
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for spec in mapping.specs:
        if not spec.extra:
            continue
        raw = _raw_value(row, spec, mapping)
        if raw in (None, ""):
            if spec.default not in (None, ""):
                extra[spec.canonical] = spec.default
            continue
        try:
            extra[spec.canonical] = _parse_scalar(raw, spec)
        except (TypeError, ValueError, OverflowError):
            issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} extra invalid; skipped", row))
    return extra


def _parse_scalar(raw: Any, spec: FieldSpec) -> Any:
    if spec.type == "str":
        return str(raw).strip()
    if spec.type == "int":
        value = int(float(raw) * spec.unit_scale)
        if not math.isfinite(float(value)):
            raise ValueError("non-finite int")
        return value
    if spec.type == "float":
        value = float(raw) * spec.unit_scale
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if spec.type == "timestamp":
        return _parse_timestamp(raw) * spec.unit_scale
    raise ValueError(f"unsupported field type: {spec.type}")


def _parse_timestamp(raw: Any) -> float:
    if raw in (None, ""):
        raise ValueError("missing timestamp")
    if isinstance(raw, (int, float)):
        return _timestamp_from_number(float(raw))
    text = str(raw).strip()
    if not text:
        raise ValueError("missing timestamp")
    try:
        return _timestamp_from_number(float(text))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return datetime.fromisoformat(text).timestamp()


def _timestamp_from_number(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("timestamp non-finite")
    if abs(value) > 10_000_000_000:
        return value / 1000.0
    return value


def _raw_value(row: dict[str, Any], spec: FieldSpec, mapping: FieldMapping) -> Any:
    column = mapping.canonical_to_source.get(spec.canonical)
    if column is None:
        return spec.default
    return row.get(column, spec.default)


def _float_default(spec: FieldSpec | None) -> float:
    if spec is None:
        return 0.0
    try:
        return float(spec.default or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _issue(
    point_ref: PointRef,
    row_number: int,
    level: str,
    reason: str,
    row: dict[str, Any],
) -> ParseIssue:
    return ParseIssue(
        point_ref=point_ref,
        row_number=row_number,
        level=level,
        reason=reason,
        raw_values={str(k): "" if v is None else str(v) for k, v in row.items()},
    )


def _check_order(previous: TrajectoryPoint | None, current: TrajectoryPoint) -> None:
    if previous is None:
        return
    if _order_key(current) < _order_key(previous):
        raise TrajectoryOrderError(
            vehicle_id=current.vehicle_id,
            previous_ref=previous.point_ref,
            current_ref=current.point_ref,
            previous_timestamp=previous.timestamp,
            current_timestamp=current.timestamp,
        )


def _order_key(point: TrajectoryPoint) -> tuple[str, float, tuple[int, str]]:
    return (point.vehicle_id, point.timestamp, _natural_ref_key(point.point_ref.source_row_key))


def _natural_ref_key(value: str) -> tuple[int, str]:
    if value.isdigit():
        return (int(value), value)
    return (0, value)


def _read_csv_columns(path: Path, encoding: str) -> list[str]:
    with open_text_auto(path, encoding=encoding, newline="") as f:
        return list(csv.DictReader(f).fieldnames or [])


def _can_decode(raw: bytes, encoding: str) -> bool:
    try:
        raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return False
    return True


def _canonical_encoding(encoding: str) -> str:
    normalized = encoding.strip().lower().replace("_", "-")
    aliases = {
        "ascii": DEFAULT_TEXT_ENCODING,
        "utf8": DEFAULT_TEXT_ENCODING,
        "utf-8": DEFAULT_TEXT_ENCODING,
        "utf-8-sig": DEFAULT_TEXT_ENCODING,
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "gb18030": "gb18030",
        "cp936": "gb18030",
    }
    return aliases.get(normalized, normalized)


def _guess_with_chardet(raw: bytes) -> tuple[str | None, float]:
    try:
        import chardet  # type: ignore[import-not-found]
    except ImportError:
        return None, 0.0
    result = chardet.detect(raw)
    return result.get("encoding"), float(result.get("confidence") or 0.0)


def _configure_postgres_session(conn: Any) -> None:
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout TO '5min'")
        cur.execute("SET idle_in_transaction_session_timeout TO '5min'")
    finally:
        _close_quietly(cur)


def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row))


def _close_quietly(resource: Any) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        close()


# ── Streaming vehicle source ────────────────────────────────────────────


class StreamVehicleSource:
    """Yield per-vehicle point lists by detecting vehicle_id changes.

    ZH: 利用 (vehicle_id, timestamp) 排序，检测 vehicle_id 变化，
        逐车 yield 完整轨迹点列表。内存 = 单车辆点数。
    """

    def __init__(self, handle: "BaseTrajectorySourceHandle") -> None:
        self._handle = handle
        self._closed = False

    @property
    def source_info(self) -> SourceInfo:
        return self._handle.source_info

    @property
    def field_mapping(self) -> FieldMapping:
        return self._handle.field_mapping

    def iter_vehicle_tracks(self) -> Iterator[list[TrajectoryPoint]]:
        """Yield one vehicle's worth of points at a time.

        Reads points one-by-one from the underlying handle.  When
        ``vehicle_id`` changes, the previous vehicle's buffered points
        are yielded and the buffer resets.
        """
        if self._closed:
            raise RuntimeError("StreamVehicleSource is closed")
        current_vid: str | None = None
        current: list[TrajectoryPoint] = []
        for point in self._handle.iter_points():
            vid = point.vehicle_id
            if current_vid is not None and vid != current_vid:
                yield current
                current = []
            current_vid = vid
            current.append(point)
        if current:
            yield current

    def close(self) -> None:
        self._closed = True
        self._handle.close()

    def __enter__(self) -> "StreamVehicleSource":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
