# Vision Runtime Provenance

The local model project was found at `../cv`. It contains the YOLOv8 weight and
Python dependency lock, but no Dockerfile and no Git commits.

The corresponding local image was identified as
`local/yolov8:8.3.0-cu117` (`sha256:3644b47127ff...`). Its Compose labels point
to project `map`, service `yolov8`; the reproducible Dockerfile and batch command
were therefore recovered from `../map/Dockerfile` and `../map/compose.yaml`.

Preserved runtime versions:

- Python 3.10.11
- PyTorch 2.0.1 with CUDA 11.7
- torchvision 0.15.2
- Ultralytics 8.3.0

The host GPU is an NVIDIA GeForce GTX 1650 with 4 GB VRAM. CPU model loading and
inference pass. GPU container startup currently fails with `failed to discover
GPU vendor from CDI`; install or repair NVIDIA Container Toolkit/CDI before
using `make vision-run`.
