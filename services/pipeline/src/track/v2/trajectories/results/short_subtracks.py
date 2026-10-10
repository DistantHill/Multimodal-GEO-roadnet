"""Bounded direct GeoParquet output for filtered Refine source observations."""
from __future__ import annotations

import json

import numpy as np
import polars as pl
from pyproj import CRS
import shapely

from ...models import RefinementSubtrack, RuntimeConfig, SpatialContext
from ...artifacts.publish_dataset import StagingDataset, _validate_handle


SHORT_SCHEMA = {
    "refine_id": pl.String, "source_id": pl.String, "vehicle_id": pl.String,
    "subtrack_index": pl.Int64, "source_partition_index": pl.Int64,
    "sequence_in_subtrack": pl.Int64, "source_row_key": pl.Int64,
    "timestamp": pl.Float64, "lon": pl.Float64, "lat": pl.Float64,
    "x": pl.Float64, "y": pl.Float64, "heading": pl.Float64,
    "speed": pl.Float64, "geometry": pl.Binary,
}


class ShortSubtrackWriter:
    """Write bounded parts into the same staging dataset as normal Refine points."""

    def __init__(self, handle: StagingDataset, runtime: RuntimeConfig,
                 spatial_context: SpatialContext) -> None:
        _validate_handle(handle)
        self.handle = handle
        self.runtime = runtime
        self.spatial_context = spatial_context
        self.point_count = self.track_count = 0
        self.files: list[str] = []
        self._pieces: list[pl.DataFrame] = []
        self._buffered = 0
        self._closed = False
        (handle.path / "short_subtrack").mkdir()

    def append(self, tracks: list[RefinementSubtrack]) -> None:
        if self._closed:
            raise ValueError("short subtrack writer is closed")
        limit = self.runtime.import_batch_size
        for track in tracks:
            if np.any(track.kind != "p") or not track.source_row_key_valid.all():
                raise ValueError("short subtracks must contain only stable source points")
            self.track_count += 1
            self.point_count += len(track)
            for start in range(0, len(track), limit):
                stop = min(start + limit, len(track))
                size = stop - start
                coordinates = track.lonlat[start:stop]
                frame = pl.DataFrame({
                    "refine_id": [str(self.handle.run_id)] * size,
                    "source_id": [track.source_id] * size,
                    "vehicle_id": [track.vehicle_id] * size,
                    "subtrack_index": [self.track_count] * size,
                    "source_partition_index": [track.subtrack_index] * size,
                    "sequence_in_subtrack": np.arange(start, stop, dtype=np.int64),
                    "source_row_key": track.source_row_key[start:stop],
                    "timestamp": track.timestamp[start:stop],
                    "lon": coordinates[:, 0], "lat": coordinates[:, 1],
                    "x": track.xy[start:stop, 0], "y": track.xy[start:stop, 1],
                    "heading": track.heading[start:stop], "speed": track.speed[start:stop],
                    "geometry": shapely.to_wkb(shapely.points(coordinates)).tolist(),
                }, schema=SHORT_SCHEMA)
                self._pieces.append(frame)
                self._buffered += size
                if self._buffered >= limit:
                    self._flush()

    def _flush(self) -> None:
        frame = pl.concat(self._pieces) if self._pieces else pl.DataFrame(schema=SHORT_SCHEMA)
        metadata = {
            "version": "1.1.0", "primary_column": "geometry",
            "columns": {"geometry": {
                "encoding": "WKB", "geometry_types": ["Point"],
                "crs": CRS.from_epsg(self.spatial_context.source_srid).to_json_dict(),
            }},
            "processing_srid": self.spatial_context.processing_srid,
        }
        relative = f"short_subtrack/part-{len(self.files):05d}.parquet"
        frame.write_parquet(
            self.handle.path / relative,
            row_group_size=self.runtime.parquet_row_group_size_rows or None,
            metadata={"geo": json.dumps(metadata, separators=(",", ":"))},
        )
        self.files.append(relative)
        self._pieces.clear()
        self._buffered = 0

    def finish(self) -> list[str]:
        if not self._closed:
            if self._pieces or not self.files:
                self._flush()
            self._closed = True
        return list(self.files)


def validate_geo_metadata(connection, paths: list[str]) -> None:
    from . import RefineParquetError
    for path in paths:
        values = dict(connection.execute("SELECT key, value FROM parquet_kv_metadata(?)", [path]).fetchall())
        try:
            metadata = json.loads(values[b"geo"])
            geometry = metadata["columns"][metadata["primary_column"]]
            if (metadata["version"] != "1.1.0" or metadata["primary_column"] != "geometry"
                    or geometry["encoding"] != "WKB" or geometry["geometry_types"] != ["Point"]):
                raise ValueError("unexpected geometry metadata")
            CRS.from_json_dict(geometry["crs"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RefineParquetError("invalid short subtrack GeoParquet metadata") from exc


def validate_short_rows(rows) -> None:
    from . import RefineParquetError
    if any(any(value is None for value in row) for row in rows):
        raise RefineParquetError("short subtrack contains null source fields")
    numbers = np.asarray([row[7:14] for row in rows], dtype=np.float64)
    if not np.isfinite(numbers).all():
        raise RefineParquetError("short subtrack contains non-finite coordinates or attributes")
    try:
        geometry = shapely.from_wkb([row[14] for row in rows])
        if (np.any(shapely.get_type_id(geometry) != 0) or shapely.is_empty(geometry).any()
                or not np.array_equal(shapely.get_coordinates(geometry), numbers[:, 1:3])):
            raise ValueError("geometry does not match source coordinates")
    except (ValueError, shapely.errors.GEOSException) as exc:
        raise RefineParquetError("invalid short subtrack point geometry") from exc
