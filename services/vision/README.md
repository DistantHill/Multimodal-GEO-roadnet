# YOLOv8 Vision Runtime

This optional batch runtime preserves the existing GTX 1650-compatible stack:
PyTorch 2.0.1, CUDA 11.7, torchvision 0.15.2, and Ultralytics 8.3.0.
Source discovery details are recorded in `SOURCE.md`.

The image performs offline detection against `/data/vision/input` and writes
annotated results to `/data/vision/output`. It is isolated from Backend request
handlers and is not required by the default development stack.

## Local use

The migrated local weight is expected at `models/yolo/yolov8n.pt` in the
repository root. The entire `models/` directory is Git-ignored.

```sh
make vision-build
make vision-check      # CPU model-load and inference smoke test
make vision-run        # Requires NVIDIA Container Toolkit / CDI
```

Place input media under `${DATA_ROOT}/vision/input`. Override inference values
with `YOLO_DEVICE`, `YOLO_IMGSZ`, `YOLO_BATCH`, `YOLO_HALF`, or `YOLO_CONF`.
For tracking exports, override the Compose command with
`python /workspace/track_export.py --source /data/vision/input/example.mp4`.

Production must set `MODEL_ROOT` and `DATA_ROOT` to directories outside the
repository. Do not copy model weights or generated media into Git.
