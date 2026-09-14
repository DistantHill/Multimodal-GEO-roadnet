"""Explicit, confirmation-gated deletion of Refine and Detect Runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


REFINE_ARTIFACT_TABLES = (
    "excluded_points",
    "kept_trajectory_points",
    "injected_points",
)

# Reverse dependency order for deletion.
DETECT_ARTIFACT_TABLES = (
    "centerline_source_clips",
    "centerlines",
    "fit_runs",
    "entry_exit_points",
    "intersections",
    "candidate_entry_exit_points",
    "turn_events",
    "tracks_cross",
    "resolved_subtrack_points",
    "candidate_intersections",
)


@dataclass(frozen=True, slots=True)
class TableCount:
    table: str
    count: int


@dataclass(frozen=True, slots=True)
class DeleteImpact:
    target_kind: str
    target_id: str
    own_rows: tuple[TableCount, ...]
    detects: tuple["DeleteImpact", ...] = ()


class DeleteRunService:
    """Preview, confirm, lock, recheck, and explicitly delete one run tree."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    def preview_detect(self, detect_id: str, *, lock: bool = False) -> DeleteImpact:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            suffix = " FOR UPDATE" if lock else ""
            cur.execute(f"SELECT id FROM detect_runs WHERE id = %s{suffix}", (detect_id,))
            if cur.fetchone() is None:
                raise ValueError(f"unknown Detect Run: {detect_id}")
        if lock:
            self._lock_artifact_tables(DETECT_ARTIFACT_TABLES)
        return self._detect_counts(detect_id)

    def preview_refine(self, refine_id: str, *, lock: bool = False) -> DeleteImpact:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            suffix = " FOR UPDATE" if lock else ""
            cur.execute(f"SELECT id FROM refine_runs WHERE id = %s{suffix}", (refine_id,))
            if cur.fetchone() is None:
                raise ValueError(f"unknown Refine Run: {refine_id}")
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            suffix = " FOR UPDATE" if lock else ""
            cur.execute(
                "SELECT id FROM detect_runs WHERE refine_id = %s ORDER BY id" + suffix,
                (refine_id,),
            )
            detect_ids = [str(row[0]) for row in cur.fetchall()]
        if lock:
            self._lock_artifact_tables(
                REFINE_ARTIFACT_TABLES + DETECT_ARTIFACT_TABLES
            )
        own_rows = self._counts(REFINE_ARTIFACT_TABLES, "refine_id", refine_id)
        return DeleteImpact(
            target_kind="refine",
            target_id=refine_id,
            own_rows=own_rows,
            detects=tuple(self._detect_counts(detect_id) for detect_id in detect_ids),
        )

    def confirm_and_delete_detect(
        self,
        detect_id: str,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> bool:
        return self._confirm_and_delete(
            self.preview_detect,
            detect_id,
            input_fn=input_fn,
            output_fn=output_fn,
        )

    def confirm_and_delete_refine(
        self,
        refine_id: str,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> bool:
        return self._confirm_and_delete(
            self.preview_refine,
            refine_id,
            input_fn=input_fn,
            output_fn=output_fn,
        )

    def _confirm_and_delete(
        self,
        preview: Callable[..., DeleteImpact],
        target_id: str,
        *,
        input_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> bool:
        original = preview(target_id)
        self._print_impact(original, output_fn)
        if input_fn("Delete this impact tree? Type y: ") != "y":
            self._conn.rollback()  # type: ignore[attr-defined]
            return False

        after_first_confirmation = preview(target_id)
        if after_first_confirmation != original:
            self._conn.rollback()  # type: ignore[attr-defined]
            raise RuntimeError("delete impact tree changed after first confirmation")

        if input_fn(f"Type the full UUID {target_id}: ") != target_id:
            self._conn.rollback()  # type: ignore[attr-defined]
            return False

        locked = preview(target_id, lock=True)
        if locked != original:
            self._conn.rollback()  # type: ignore[attr-defined]
            raise RuntimeError("delete impact tree changed after UUID confirmation")

        # Completed Detect/Fit artifacts are immutable during ordinary DML.
        # This confirmation-gated service is the sole intentional deletion
        # path, so scope the bypass to this transaction only.
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "SET LOCAL track_v2.allow_completed_run_delete = 'on'",
                (),
            )

        if locked.target_kind == "detect":
            self._delete_detect(locked.target_id)
        else:
            for detect in locked.detects:
                self._delete_detect(detect.target_id)
            for table in REFINE_ARTIFACT_TABLES:
                self._delete_rows(table, "refine_id", locked.target_id)
            self._delete_rows("refine_runs", "id", locked.target_id)
        self._conn.commit()  # type: ignore[attr-defined]
        return True

    def _detect_counts(self, detect_id: str) -> DeleteImpact:
        fit_rows = (
            self._count_centerline_source_clips(detect_id),
            *self._counts(("centerlines", "fit_runs"), "detect_id", detect_id),
        )
        return DeleteImpact(
            target_kind="detect",
            target_id=detect_id,
            own_rows=fit_rows + self._counts(
                DETECT_ARTIFACT_TABLES[3:], "detect_id", detect_id,
            ),
        )

    def _count_centerline_source_clips(self, detect_id: str) -> TableCount:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "SELECT COUNT(*) FROM centerline_source_clips AS source_clips "
                "JOIN centerlines AS c ON c.id = source_clips.centerline_id "
                "WHERE c.detect_id = %s",
                (detect_id,),
            )
            return TableCount("centerline_source_clips", int(cur.fetchone()[0]))

    def _counts(
        self,
        tables: tuple[str, ...],
        column: str,
        target_id: str,
    ) -> tuple[TableCount, ...]:
        rows: list[TableCount] = []
        for table in tables:
            with self._conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", (target_id,))
                rows.append(TableCount(table, int(cur.fetchone()[0])))
        return tuple(rows)

    def _lock_artifact_tables(
        self,
        tables: tuple[str, ...],
    ) -> None:
        for table in tables:
            with self._conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(
                    f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE",
                    (),
                )

    def _delete_detect(self, detect_id: str) -> None:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "DELETE FROM centerline_source_clips WHERE centerline_id IN "
                "(SELECT id FROM centerlines WHERE detect_id = %s)",
                (detect_id,),
            )
        for table in DETECT_ARTIFACT_TABLES[1:]:
            self._delete_rows(table, "detect_id", detect_id)
        self._delete_rows("detect_runs", "id", detect_id)

    def _delete_rows(self, table: str, column: str, target_id: str) -> None:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (target_id,))

    @staticmethod
    def _print_impact(impact: DeleteImpact, output_fn: Callable[[str], None]) -> None:
        for row in impact.own_rows:
            output_fn(f"{row.table}: {row.count}")
        for detect in impact.detects:
            output_fn(f"Detect Run {detect.target_id}")
            for row in detect.own_rows:
                output_fn(f"{row.table}: {row.count}")
