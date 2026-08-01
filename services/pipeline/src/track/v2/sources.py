from __future__ import annotations

import csv
import hashlib
import json
import math
import uuid

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

    def count_vehicle_ids(self) -> int:
        """ZH: 快速统计 distinct vehicle_id 数量，用于进度条显示。
            不完整解析所有字段，比 iter_points() 快得多。
        EN: Fast count of distinct vehicle_ids for progress bar display.
            Does not fully parse all fields — much faster than iter_points()."""
        raise NotImplementedError

    def count_rows(self) -> int:
        """ZH: 快速统计总行数，用于行计数进度条。
            不解析任何字段，比 count_vehicle_ids() 更快。
        EN: Fast row count for row-based progress bar.
            Parses no fields — faster than count_vehicle_ids()."""
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
                point_ref = PointRef(
                    str(self._path.resolve()),
                    str(row_number),
                    True,
                    source_row_hash=source_row_hash(row),
                )
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

    def count_vehicle_ids(self) -> int:
        """ZH: 快速扫描 CSV，仅提取 vehicle_id 列统计 distinct 数量。
        EN: Fast CSV scan — only reads vehicle_id column to count distinct values."""
        vehicle_col = self._mapping.canonical_to_source.get("vehicle_id", "vehicle_id")
        prev_vid: str | None = None
        count = 0
        with open_text_auto(self._path, encoding=self._encoding, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = (row.get(vehicle_col) or "").strip()
                if vid and vid != prev_vid:
                    prev_vid = vid
                    count += 1
        return count

    def count_rows(self) -> int:
        """ZH: 快速扫描 CSV，统计总行数。比 count_vehicle_ids() 更简单——纯行计数。
        EN: Fast CSV scan — counts all rows. Simpler than count_vehicle_ids()."""
        count = 0
        with open_text_auto(self._path, encoding=self._encoding, newline="") as f:
            reader = csv.DictReader(f)
            for _row in reader:
                count += 1
        return count


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
            # ZH: 服务器端命名游标 — PG 按需 FETCH，不再全量物化到 portal。
            # EN: Server-side named cursor — PG FETCHes on demand instead of
            #     materializing the full result into a portal.
            cur = conn.cursor(
                name=f"iter_pts_{uuid.uuid4().hex}",
            )
            # ZH: psycopg3 默认 itersize=100（非 psycopg2 的 2000），1M 行=10k 次 FETCH。
            #     增大到 10000 减少 ~100x 往返；itersize 是 mutable 属性，非构造参数。
            # EN: psycopg3 defaults to itersize=100 (not 2000 like psycopg2).
            #     Bump to 10k to cut FETCH round-trips ~100x; itersize is mutable.
            cur.itersize = 10_000
            cur.execute(self._source.query, self._source.params)
            columns = [desc[0] for desc in (cur.description or [])]

            # ── 预计算：列名→位置，用于元组快速路径跳过 dict 分配 ──
            #     Pre-compute column→position index for tuple fast-path
            #     (avoids per-row _row_to_dict + _raw_value double lookup).
            col_index: dict[str, int] = {name: i for i, name in enumerate(columns)}
            specs = self._mapping.specs_by_canonical
            # 构建 canonical→tuple_index 的子集（仅含 spec 中定义的 canonical 字段）
            _fast_col_index: dict[str, int] = {}
            for spec in self._mapping.specs:
                src_col = self._mapping.canonical_to_source.get(spec.canonical)
                if src_col and src_col in col_index:
                    _fast_col_index[spec.canonical] = col_index[src_col]

            # PK 列索引（用于 _point_ref）
            pk_field = self._source.primary_key_field
            pk_idx: int | None = col_index.get(pk_field) if pk_field else None
            source_id = self._source_id()

            for ordinal, row in enumerate(cur, start=1):
                self._state.total_rows += 1
                try:
                    point, issues = _parse_row_from_tuple(
                        row, ordinal, _fast_col_index, specs, source_id, pk_idx,
                    )
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

    def count_vehicle_ids(self) -> int:
        """ZH: 通过 COUNT(DISTINCT vehicle_id) 查询统计车辆数。
        EN: Query COUNT(DISTINCT vehicle_id) for vehicle count."""
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            count_sql = (
                f"SELECT COUNT(DISTINCT vehicle_id) FROM ({self._source.query}) AS _count_sub"
            )
            cur.execute(count_sql, self._source.params)
            result = cur.fetchone()
            return int(result[0]) if result else 0
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    def count_rows(self) -> int:
        """ZH: 从 pg_class 读取估算行数——纯元数据查询，毫秒级。
            用于进度条百分比，不必精确到个位数。
        EN: Read estimated row count from pg_class — pure metadata query, sub-ms.
            Used for progress bar percentage; exact precision is unnecessary."""
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            table = self._source.source_id or "trajectory_points"
            cur.execute(
                "SELECT c.reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND n.nspname = current_schema()",
                (table,),
            )
            result = cur.fetchone()
            return int(result[0]) if result and result[0] else 0
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    def _discover_columns(self) -> list[str]:
        """ZH: 从 information_schema 读取列名——纯元数据查询，不碰数据行。
        EN: Read column names from information_schema — pure metadata query, zero data rows touched."""
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            table = self._source.source_id or "trajectory_points"
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (table,),
            )
            return [row[0] for row in cur.fetchall()]
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    def _point_ref(self, row: dict[str, Any], ordinal: int) -> PointRef:
        # ZH: PG 模式下 source_row_key 就是 DB 主键，稳定且可复现，
        #     不需要昂贵的 SHA-256 哈希做 replay 校验——跳过以提速。
        # EN: In PG mode source_row_key is the DB PK, already stable and
        #     reproducible — skip the expensive SHA-256 hash to improve throughput.
        key = self._source.primary_key_field
        if key in row and row[key] not in (None, ""):
            return PointRef(
                self._source_id(),
                str(row[key]),
                True,
            )
        return PointRef(
            self._source_id(),
            str(ordinal),
            False,
        )

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


def _parse_row_from_tuple(
    row: tuple,
    ordinal: int,
    col_index: dict[str, int],       # canonical → tuple position
    specs: dict[str, FieldSpec],
    source_id: str,
    pk_idx: int | None,
) -> tuple[TrajectoryPoint, list[ParseIssue]]:
    """ZH: PG 元组快速解析路径——跳过 _row_to_dict dict 分配 + _raw_value 双 dict 查找，
        直接用 tuple[row_idx] 索引。保留 parse_row 全部校验语义。
    EN: Fast-path tuple parser for PG rows — skips per-row dict allocation and
        double dict lookup in _raw_value. Preserves all parse_row validation.
    """
    issues: list[ParseIssue] = []

    # ── PointRef from tuple PK column ──
    if pk_idx is not None:
        pk_val = row[pk_idx]
        if pk_val not in (None, ""):
            point_ref = PointRef(source_id, str(pk_val), True)
        else:
            point_ref = PointRef(source_id, str(ordinal), False)
    else:
        point_ref = PointRef(source_id, str(ordinal), False)

    def _fast_issue(level: str, reason: str) -> ParseIssue:
        return ParseIssue(
            point_ref=point_ref, row_number=ordinal,
            level=level, reason=reason, raw_values={},
        )

    # ── vehicle_id (required str) ──
    vid_spec = specs.get("vehicle_id")
    if vid_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "vehicle_id spec not found"))
    vid_raw = row[col_index["vehicle_id"]] if "vehicle_id" in col_index else None
    vehicle_id = "" if vid_raw is None else str(vid_raw).strip()
    if not vehicle_id:
        raise _SkipRow(_fast_issue("ERROR", "vehicle_id missing"))

    # ── timestamp (required) ──
    ts_spec = specs.get("timestamp")
    if ts_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "timestamp spec not found"))
    ts_raw = row[col_index["timestamp"]] if "timestamp" in col_index else None
    try:
        timestamp = _parse_timestamp(ts_raw) * ts_spec.unit_scale
    except (TypeError, ValueError, OverflowError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "timestamp invalid")) from exc
    if not math.isfinite(timestamp):
        raise _SkipRow(_fast_issue("ERROR", "timestamp non-finite"))

    # ── lon (required float) ──
    lon_spec = specs.get("lon")
    if lon_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "lon spec not found"))
    lon_raw = row[col_index["lon"]] if "lon" in col_index else None
    try:
        lon = float(lon_raw) * lon_spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "lon invalid")) from exc
    if not math.isfinite(lon):
        raise _SkipRow(_fast_issue("ERROR", "lon non-finite"))
    if not -180.0 <= lon <= 180.0:
        raise _SkipRow(_fast_issue("ERROR", "lon out of bounds"))

    # ── lat (required float) ──
    lat_spec = specs.get("lat")
    if lat_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "lat spec not found"))
    lat_raw = row[col_index["lat"]] if "lat" in col_index else None
    try:
        lat = float(lat_raw) * lat_spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "lat invalid")) from exc
    if not math.isfinite(lat):
        raise _SkipRow(_fast_issue("ERROR", "lat non-finite"))
    if not -90.0 <= lat <= 90.0:
        raise _SkipRow(_fast_issue("ERROR", "lat out of bounds"))

    # ── heading (optional float) ──
    hdg_spec = specs.get("heading")
    heading: float = 0.0
    if hdg_spec is not None and "heading" in col_index:
        hdg_raw = row[col_index["heading"]]
        if hdg_raw in (None, ""):
            heading = _float_default(hdg_spec)
        else:
            try:
                heading = float(hdg_raw) * hdg_spec.unit_scale
            except (TypeError, ValueError):
                heading = _float_default(hdg_spec)
                issues.append(_fast_issue("WARN", "heading invalid; using default"))
    if not math.isfinite(heading):
        heading = _float_default(hdg_spec) if hdg_spec else 0.0
        issues.append(_fast_issue("WARN", "heading non-finite; using default"))
    elif not 0.0 <= heading < 360.0:
        heading %= 360.0
        issues.append(_fast_issue("WARN", "heading normalized to [0, 360)"))

    # ── speed (optional float) ──
    spd_spec = specs.get("speed")
    speed: float = 0.0
    if spd_spec is not None and "speed" in col_index:
        spd_raw = row[col_index["speed"]]
        if spd_raw in (None, ""):
            speed = _float_default(spd_spec)
        else:
            try:
                speed = float(spd_raw) * spd_spec.unit_scale
            except (TypeError, ValueError):
                speed = _float_default(spd_spec)
                issues.append(_fast_issue("WARN", "speed invalid; using default"))
    if not math.isfinite(speed) or speed < 0.0:
        speed = _float_default(spd_spec) if spd_spec else 0.0
        issues.append(_fast_issue("WARN", "speed invalid; using default"))

    point = TrajectoryPoint(
        point_ref=point_ref,
        vehicle_id=vehicle_id,
        timestamp=timestamp,
        lon=lon,
        lat=lat,
        heading=heading,
        speed=speed,
        extra={},
    )
    return point, issues


def parse_row(
    row: dict[str, Any],
    point_ref: PointRef,
    row_number: int,
    mapping: FieldMapping,
) -> tuple[TrajectoryPoint, list[ParseIssue]]:
    specs = mapping.specs_by_canonical
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
    """ZH: 配置 PostgreSQL 会话——禁用超时以适应 named cursor 的长事务。

    Named cursor (DECLARE CURSOR) 会持有一个事务直到游标关闭。如果
    idle_in_transaction_session_timeout 有值，服务器会在 FETCH 间隔空闲超时后
    杀死连接。对于数百万行的迭代，禁用超时是正确做法——这不是意外僵死事务，
    而是故意的长事务。

    EN: Configure PostgreSQL session — disable timeouts for named-cursor
    long transactions. A named cursor holds a transaction open until the
    cursor closes. With idle_in_transaction_session_timeout set, the server
    kills the connection when idle between FETCH calls. For multi-million-row
    iteration, disabling the timeout is correct — this is a deliberate
    long transaction, not a stuck one.
    """
    cur = None
    try:
        cur = conn.cursor()
        # ZH: statement_timeout=0 禁用它——每个 FETCH 非常快，但 system load 下可能波动。
        # EN: Disable statement_timeout=0 — each FETCH is fast but may spike under load.
        cur.execute("SET statement_timeout TO '0'")
        # ZH: 关键修复：禁用 idle_in_transaction_session_timeout。
        #     Named cursor 的事务贯穿整个迭代（可能 30+ 分钟），
        #     有超时的话 PG server 会在空闲间隙杀死连接。
        # EN: Key fix: disable idle_in_transaction_session_timeout.
        #     The named cursor's transaction spans the entire iteration
        #     (potentially 30+ min); with a timeout the PG server kills
        #     the connection during idle gaps between FETCH calls.
        cur.execute("SET idle_in_transaction_session_timeout TO '0'")
    finally:
        _close_quietly(cur)


def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row))


def source_row_hash(row: dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for one raw source row.

    Replay stores only this fingerprint, then validates a live source row
    before exposing it.  The source payload itself never lands in the replay
    metadata.
    """
    normalized = {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
