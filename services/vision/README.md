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

## Polygon raster segmentation

Prepare buffered rasters with the Pipeline `rasterize --polygons --buffer-m 300`
command. Pixels remain 10 m; original polygons remain unbuffered segmentation
targets. Use `--polygon-label TP` for the held-out Zhuangli GeoPackage.

Run `prepare_polygon_segmentation.py` in the Pipeline Python environment:

```sh
python prepare_polygon_segmentation.py \
  --training-raster /data/raster/polygon-samples/TRAIN_RUN \
  --zhuangli-raster /data/raster/polygon-samples/ZHUANGLI_RUN \
  --output /data/vision/intersection-seg/dataset
```

TP polygons become class 0 (`intersection`). FP polygons are not foreground.
Every known neighbouring TP within each image window is included and clipped to
that window, including TP neighbours of an FP target. Only windows without any
known TP receive empty YOLO label files. Exact duplicate TP geometry is removed
from the label catalog; original raster samples remain present.

Overlapping raster windows form connected components assigned wholly to either
training or internal validation. Zhuangli windows cannot overlap those windows
and remain a separate test set. YOLO polygon text cannot preserve holes, so the
exporter rejects such labels instead of silently filling them. The raster masks
still preserve holes. Dataset manifests record all assignments and source IDs.

Download the official [YOLOv8n-seg checkpoint](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n-seg.pt)
outside the repository, then run the training script in the existing Vision image:

```sh
python train_polygon_segmentation.py \
  --dataset /data/vision/intersection-seg/dataset \
  --weights /models/yolov8n-seg.pt \
  --output /data/vision/intersection-seg/run \
  --device 0 --epochs 100 --imgsz 256 --batch 4
```

The GTX 1650 configuration uses FP32, zero data-loader workers, no training plots,
and a batch of four. Set a 3 GiB container memory limit to bound host RAM as well
as using the small GPU workload. Early stopping uses 25 epochs of patience on
internal validation; Zhuangli never selects the checkpoint. The best checkpoint
is evaluated on the test split and produces JSON metrics, per-window predictions,
and overlays (green truth, magenta prediction). Reports include weight hashes,
library versions and peak allocated GPU memory. Empty detections remain in the
report with their false-negative counts.

The dataset YAML records its absolute container root. When relocating the
dataset, update `path` in `data.yaml` to its new root. Keep the directory layout
below that root unchanged.

Run dataset tests with the Pipeline environment:

```sh
python -m unittest discover -s services/vision/tests -v
```

Production must set `MODEL_ROOT` and `DATA_ROOT` to directories outside the
repository. Do not copy model weights or generated media into Git.
