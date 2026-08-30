FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

ARG ULTRALYTICS_VERSION=8.3.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install "numpy<2" "ultralytics==${ULTRALYTICS_VERSION}"

WORKDIR /workspace

COPY track_export.py ./track_export.py
COPY trackers ./trackers

CMD ["yolo", "predict", "model=/models/yolo/yolov8n.pt", "source=/data/vision/input", "device=0", "imgsz=640", "batch=1", "half=True", "conf=0.25", "save=True", "project=/data/vision/output", "name=yolov8n-640", "exist_ok=True"]
