import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from track.v2.clip import extract_clips_for_detect
from track.v2.config import connect, load_pipeline_config

DETECT_ID = "a06566cc-c860-4efd-9ae2-c536a7d1477b"
PAIRS = {(55,79), (79,55)}


def main() -> None:
    config = load_pipeline_config(
        os.environ["PIPELINE_CONFIG_PATH"],
        postgres_password=os.environ["POSTGRES_PASSWORD"],
    )
    runtime = config.runtime
    data_root = config.paths.data_root
    publication_root = config.paths.parquet_directory
    with connect(config.postgres) as conn:
        result = extract_clips_for_detect(
            conn, DETECT_ID, publication_root=publication_root,
            data_root=data_root, runtime=runtime,
        )

    clips = [
        clip
        for clip in result.clips
        if clip.clip_type == "between_intersection"
        and (clip.start.intersection_id, clip.end.intersection_id) in PAIRS
    ]

    print(f"找到 {len(clips)} 条 Clip")

    for clip in clips:
        coordinates = [(clip.start.lon, clip.start.lat)]
        coordinates.extend((point.lon, point.lat) for point in clip.points)
        coordinates.append((clip.end.lon, clip.end.lat))

        print(
            clip.vehicle_id,
            f"{clip.start.intersection_id} -> {clip.end.intersection_id}",
            f"distance={clip.distance_m:.1f}m",
            f"points={clip.point_count}",
            f"crossings={clip.start.crossing_id} -> {clip.end.crossing_id}",
        )


if __name__ == "__main__":
    main()
