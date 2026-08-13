"""Explicit, confirmation-gated deletion of Clean and Detect Runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


CLEAN_ARTIFACT_TABLES = (
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
    "candidate_intersection_events",
    "candidate_intersections",
    "turn_events",
    "tracks_cross",
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
        return self._detect_counts(detect_id)

    def preview_clean(self, clean_id: str, *, lock: bool = False) -> DeleteImpact:
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            suffix = " FOR UPDATE" if lock else ""
            cur.execute(f"SELECT id FROM clean_runs WHERE id = %s{suffix}", (clean_id,))
            if cur.fetchone() is None:
                raise ValueError(f"unknown Clean Run: {clean_id}")
        own_rows = self._counts(CLEAN_ARTIFACT_TABLES, "clean_id", clean_id)
        with self._conn.cursor() as cur:  # type: ignore[attr-defined]
            suffix = " FOR UPDATE" if lock else ""
            cur.execute(
                "SELECT id FROM detect_runs WHERE clean_id = %s ORDER BY id" + suffix,
                (clean_id,),
            )
            detect_ids = [str(row[0]) for row in cur.fetchall()]
        return DeleteImpact(
            target_kind="clean",
            target_id=clean_id,
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

    def confirm_and_delete_clean(
        self,
        clean_id: str,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> bool:
        return self._confirm_and_delete(
            self.preview_clean,
            clean_id,
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

        if locked.target_kind == "detect":
            self._delete_detect(locked.target_id)
        else:
            for detect in locked.detects:
                self._delete_detect(detect.target_id)
            for table in CLEAN_ARTIFACT_TABLES:
                self._delete_rows(table, "clean_id", locked.target_id)
            self._delete_rows("clean_runs", "id", locked.target_id)
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
