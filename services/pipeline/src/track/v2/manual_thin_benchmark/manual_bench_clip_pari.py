import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from track.v2.clip import extract_clips_for_run
from track.v2.config import connect, default_postgres_config

RUN_ID = "a06566cc-c860-4efd-9ae2-c536a7d1477b"
PAIRS = {(55,79), (79,55)}


def main() -> None:
    with connect(default_postgres_config()) as conn:
        result = extract_clips_for_run(conn, RUN_ID)

    clips = [
        clip
        for clip in result.clips
        if clip.clip_type == "inter_intersection"
        and (clip.start.cluster_id, clip.end.cluster_id) in PAIRS
    ]

    print(f"找到 {len(clips)} 条 Clip")

    for clip in clips:
        coordinates = [(clip.start.lon, clip.start.lat)]
        coordinates.extend((point.lon, point.lat) for point in clip.points)
        coordinates.append((clip.end.lon, clip.end.lat))

        print(
            clip.vehicle_id,
            f"{clip.start.cluster_id} -> {clip.end.cluster_id}",
            f"distance={clip.distance_m:.1f}m",
            f"points={clip.point_count}",
            f"time={clip.start.timestamp} -> {clip.end.timestamp}",
        )


if __name__ == "__main__":
    main()
