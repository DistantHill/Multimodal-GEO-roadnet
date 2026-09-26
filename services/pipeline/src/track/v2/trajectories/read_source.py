"""Read CSV and PostgreSQL trajectory sources as ordered point streams."""

from __future__ import annotations

import csv
import hashlib
import json
import uuid
import warnings
import numpy as np
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..errors import (
    DataSourceCorrupted,
    EncodingDetectionError,
    ResourceLeakWarning,
    TrajectoryOrderError,
    V2Error,
)
from ..models import (
    ColumnSelector,
    CsvSource,
    FieldConfig,
    FieldMapping,
    ParseIssue,
    PostgresSource,
    ReadReport,
    RuntimeConfig,
    SourceInfo,
    SourceBatch,
)
from .parse_rows import (
    _SkipRow,
    _parse_row_from_tuple,
    parse_row,
)


_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)
def get_database(
    source: CsvSource | PostgresSource,
    *,
    field_config: FieldConfig,
    runtime: RuntimeConfig,
    selector: ColumnSelector | None = None,
) -> "BaseTrajectorySourceHandle":
    config = field_config
    if runtime.csv_max_field_size > csv.field_size_limit():
        csv.field_size_limit(runtime.csv_max_field_size)
    if isinstance(source, CsvSource):
        return CsvTrajectorySourceHandle(source, config, selector, runtime)
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

    def _iter_values(self) -> Iterator[tuple]:
        raise NotImplementedError

    def iter_vehicle_batches(self) -> Iterator[SourceBatch]:
        if self._closed:
            raise RuntimeError("trajectory source handle is closed")
        columns: list[list] = [[] for _ in range(10)]
        vehicle_id: str | None = None
        extra_columns: dict[str, list] = {}
        source_id = self._source_info.path_or_dsn
        for values in self._iter_values():
            if vehicle_id is not None and values[0] != vehicle_id:
                yield _source_batch(source_id, vehicle_id, columns, extra_columns)
                columns = [[] for _ in range(10)]
                extra_columns = {}
            vehicle_id = values[0]
            extras = values[6] or {}
            previous_size = len(columns[0])
            for name in extras:
                if name not in extra_columns:
                    extra_columns[name] = [None] * previous_size
            for name, column in extra_columns.items():
                column.append(extras.get(name))
            for index, (column, value) in enumerate(zip(columns, values)):
                if index != 6:
                    column.append(value)
        if vehicle_id is not None:
            yield _source_batch(source_id, vehicle_id, columns, extra_columns)

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
        runtime: RuntimeConfig,
    ) -> None:
        self._source = source
        self._path = Path(source.path)
        self._encoding = detect_encoding(
            self._path,
            hint_encoding=source.encoding,
            sample_size=runtime.encoding_sample_size,
            min_confidence=runtime.encoding_confidence,
            default_encoding=runtime.default_text_encoding,
            fallback=runtime.fallback_encodings,
        )
        columns = _read_csv_columns(self._path, self._encoding)
        source_info = SourceInfo(
            kind="csv",
            path_or_dsn=str(self._path.resolve()),
            encoding=self._encoding,
            column_count=len(columns),
            columns=columns,
        )
        from ..config.field_mapping import build_field_mapping

        mapping = build_field_mapping(config.specs, columns, selector)
        super().__init__(source_info, config, mapping)

    def _iter_values(self) -> Iterator[tuple]:
        self._state = self._new_state()
        previous: tuple | None = None
        with open_text_auto(self._path, encoding=self._encoding, newline="") as f:
            reader = csv.DictReader(f)
            for row_number, row in enumerate(reader, start=2):
                self._state.total_rows += 1
                point_ref = str(row_number)
                try:
                    point, issues = parse_row(row, point_ref, row_number, self._mapping)
                except _SkipRow as exc:
                    self._state.add_issue(exc.issue)
                    self._state.assert_thresholds()
                    continue

                for issue in issues:
                    self._state.add_issue(issue)
                _check_order(previous, (*point, point_ref))
                previous = (*point, point_ref)
                self._state.mark_loaded()
                yield (*point, row_number, True, source_row_hash(row))
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
        from ..config.field_mapping import build_field_mapping

        mapping = build_field_mapping(config.specs, columns, selector)
        super().__init__(source_info, config, mapping)

    @observed_operation("读取源数据与逐车精炼")
    def _iter_values(self) -> Iterator[tuple]:
        self._state = self._new_state()
        previous: tuple | None = None
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
                if ordinal % 10_000 == 0:
                    heartbeat_progress(ordinal)
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
                source_key = str(row[pk_idx]) if pk_idx is not None and row[pk_idx] is not None else str(ordinal)
                _check_order(previous, (*point, source_key))
                previous = (*point, source_key)
                self._state.mark_loaded()
                key = row[pk_idx] if pk_idx is not None else None
                yield (*point, int(key) if key not in (None, "") else ordinal,
                       key not in (None, ""), "")
                self._state.assert_thresholds()
            self._state.assert_thresholds()
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

    @observed_operation("统计源数据车辆数")
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
            通过 to_regclass 解析 source_id，兼容 schema-qualified 表名。
            用于进度条百分比，不必精确到个位数。
        EN: Read estimated row count from pg_class — pure metadata query, sub-ms.
            Resolve source_id with to_regclass so schema-qualified table names work.
            Used for progress bar percentage; exact precision is unnecessary."""
        conn = self._connect()
        cur = None
        try:
            _configure_postgres_session(conn)
            cur = conn.cursor()
            table = self._source.source_id or "trajectory_points"
            cur.execute(
                "SELECT c.reltuples::bigint FROM pg_class c "
                "WHERE c.oid = to_regclass(%s)",
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
            query = self._source.query.rstrip().removesuffix(";")
            cur.execute(
                f"SELECT * FROM ({query}) AS source_columns LIMIT 0",
                self._source.params,
            )
            return [desc[0] for desc in (cur.description or [])]
        finally:
            _close_quietly(cur)
            _close_quietly(conn)

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


def detect_encoding(
    file_path: str | Path,
    *,
    hint_encoding: str | None,
    sample_size: int,
    min_confidence: float,
    default_encoding: str,
    fallback: tuple[str, ...],
) -> str:
    path = Path(file_path)
    with path.open("rb") as f:
        raw = f.read(sample_size)
    if not raw:
        return hint_encoding or default_encoding

    for prefix, encoding in _BOM_ENCODINGS:
        if raw.startswith(prefix):
            return encoding

    if hint_encoding is not None and _can_decode(raw, hint_encoding):
        return hint_encoding
    if _can_decode(raw, default_encoding):
        return default_encoding

    guessed, confidence = _guess_with_chardet(raw)
    if guessed is not None and confidence >= min_confidence:
        encoding = _canonical_encoding(guessed)
        if _can_decode(raw, encoding):
            return encoding

    for encoding in fallback:
        if _can_decode(raw, encoding):
            return encoding

    raise EncodingDetectionError(f"unable to detect text encoding for {path}")


def open_text_auto(
    file_path: str | Path,
    *,
    encoding: str,
    newline: str | None = None,
    errors: str = "strict",
) -> TextIO:
    path = Path(file_path)
    return path.open("r", encoding=encoding, newline=newline, errors=errors)


def _check_order(previous: tuple | None, current: tuple) -> None:
    if previous is not None and (current[0], current[1]) < (previous[0], previous[1]):
        raise TrajectoryOrderError(vehicle_id=current[0], previous_ref=previous[7],
                                   current_ref=current[7], previous_timestamp=previous[1],
                                   current_timestamp=current[1])


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
    """Allow pauses between FETCH calls only for this reader transaction.

    statement_timeout remains under the caller/database configuration: it
    limits executing SQL, not the idle time spent processing fetched rows.
    SET LOCAL prevents this exception leaking into later write transactions.
    """
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL idle_in_transaction_session_timeout TO '0'")
    finally:
        _close_quietly(cur)


def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row))


def source_row_hash(row: dict[str, Any]) -> str:
    """Return a stable identity hash used by the standalone CSV parser.

    This remains part of ``CsvSource``/importer identity handling; schema-v4
    database replay validates ``refine_runs.source_fingerprint`` instead.
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

    def iter_vehicle_tracks(self) -> Iterator[SourceBatch]:
        if self._closed:
            raise RuntimeError("StreamVehicleSource is closed")
        yield from self._handle.iter_vehicle_batches()

    def close(self) -> None:
        self._closed = True
        self._handle.close()

    def __enter__(self) -> "StreamVehicleSource":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _source_batch(source_id: str, vehicle_id: str, columns: list[list],
                  extra_columns: dict[str, list]) -> SourceBatch:
    return SourceBatch(
        source_id, vehicle_id,
        np.column_stack((columns[2], columns[3])).astype(np.float64),
        np.asarray(columns[1], dtype=np.float64),
        np.asarray(columns[4], dtype=np.float64),
        np.asarray(columns[5], dtype=np.float64),
        np.asarray(columns[7], dtype=np.int64),
        np.asarray(columns[8], dtype=np.bool_),
        np.asarray(columns[9], dtype=object),
        {name: np.asarray(values, dtype=object) for name, values in extra_columns.items()},
    )
