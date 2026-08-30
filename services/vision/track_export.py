#!/usr/bin/env python3
import argparse
import csv
import json
from collections import defaultdict, deque
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO


FIELDS = [
    "frame",
    "timestamp_ms",
    "x",
    "y",
    "id",
    "type",
    "class_id",
    "confidence",
    "center_x",
    "center_y",
    "bottom_center_x",
    "bottom_center_y",
    "x1",
    "y1",
    "x2",
    "y2",
]


def parse_args():
    parser = argparse.ArgumentParser(description="YOLOv8 video tracking and pixel trajectory export")
    parser.add_argument("--source", required=True, help="Input video path")
    parser.add_argument("--model", default="/workspace/weights/yolov8n.pt")
    parser.add_argument("--output", default="/workspace/output/tracks")
    parser.add_argument("--point", choices=("center", "bottom_center"), default="bottom_center")
    parser.add_argument("--tracker", default="/workspace/trackers/bytetrack-custom.yaml")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--conf", type=float, default=0.1)
    parser.add_argument("--device", default="0")
    return parser.parse_args()


def color_for(track_id):
    return (
        int((37 * track_id + 80) % 255),
        int((17 * track_id + 160) % 255),
        int((29 * track_id + 240) % 255),
    )


def main():
    args = parse_args()
    source = Path(args.source)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise SystemExit(f"Cannot open video: {source}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    stem = source.stem
    video_path = output_dir / f"{stem}_tracked.mp4"
    csv_path = output_dir / f"{stem}_tracks.csv"
    jsonl_path = output_dir / f"{stem}_tracks.jsonl"

    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise SystemExit(f"Cannot create output video: {video_path}")

    model = YOLO(args.model)
    histories = defaultdict(lambda: deque(maxlen=60))
    frame_index = 0

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file, jsonl_path.open(
        "w", encoding="utf-8"
    ) as jsonl_file:
        csv_writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
        csv_writer.writeheader()

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            result = model.track(
                frame,
                persist=True,
                tracker=args.tracker,
                device=args.device,
                imgsz=args.imgsz,
                conf=args.conf,
                half=True,
                verbose=False,
            )[0]
            annotated = result.plot()
            timestamp_ms = round(frame_index * 1000.0 / fps, 3)

            boxes = result.boxes
            if boxes is not None and boxes.id is not None:
                xyxy = boxes.xyxy.cpu().numpy()
                track_ids = boxes.id.int().cpu().tolist()
                class_ids = boxes.cls.int().cpu().tolist()
                confidences = boxes.conf.cpu().tolist()

                for bounds, track_id, class_id, confidence in zip(
                    xyxy, track_ids, class_ids, confidences
                ):
                    x1, y1, x2, y2 = (float(value) for value in bounds)
                    center_x = (x1 + x2) / 2.0
                    center_y = (y1 + y2) / 2.0
                    bottom_x = center_x
                    bottom_y = y2
                    point_x, point_y = (
                        (center_x, center_y)
                        if args.point == "center"
                        else (bottom_x, bottom_y)
                    )
                    class_name = model.names[class_id]
                    row = {
                        "frame": frame_index,
                        "timestamp_ms": timestamp_ms,
                        "x": round(point_x, 2),
                        "y": round(point_y, 2),
                        "id": track_id,
                        "type": class_name,
                        "class_id": class_id,
                        "confidence": round(float(confidence), 6),
                        "center_x": round(center_x, 2),
                        "center_y": round(center_y, 2),
                        "bottom_center_x": round(bottom_x, 2),
                        "bottom_center_y": round(bottom_y, 2),
                        "x1": round(x1, 2),
                        "y1": round(y1, 2),
                        "x2": round(x2, 2),
                        "y2": round(y2, 2),
                    }
                    csv_writer.writerow(row)
                    jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")

                    histories[track_id].append((int(point_x), int(point_y)))
                    points = np.asarray(histories[track_id], dtype=np.int32).reshape((-1, 1, 2))
                    if len(points) > 1:
                        cv2.polylines(
                            annotated,
                            [points],
                            isClosed=False,
                            color=color_for(track_id),
                            thickness=2,
                        )

            writer.write(annotated)
            frame_index += 1

    cap.release()
    writer.release()
    print(f"Annotated video: {video_path}")
    print(f"CSV tracks:      {csv_path}")
    print(f"JSONL tracks:    {jsonl_path}")


if __name__ == "__main__":
    main()
