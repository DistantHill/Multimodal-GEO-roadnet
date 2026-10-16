"""Train YOLOv8n-seg and evaluate the untouched Zhuangli test windows."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import traceback

import cv2
import numpy as np
from PIL import Image, ImageDraw
import torch
import ultralytics
from ultralytics import YOLO


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def label_masks(path, width, height):
    masks = []
    for line in path.read_text().splitlines():
        numbers = [float(v) for v in line.split()]
        if not numbers:
            continue
        points = np.asarray(numbers[1:]).reshape(-1, 2) * [width, height]
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(points).astype(np.int32)], 1)
        masks.append(mask.astype(bool))
    return masks


def evaluate_windows(model, dataset, output, device, imgsz):
    from scipy.optimize import linear_sum_assignment

    manifest = json.loads((dataset / 'dataset_manifest.json').read_text())
    results = []
    previews = []
    for sample in [row for row in manifest['samples'] if row['split'] == 'test']:
        source = dataset / sample['image']
        prediction = model.predict(str(source), imgsz=imgsz, device=device, conf=.25,
                                   iou=.7, retina_masks=True, verbose=False)[0]
        with Image.open(source) as image:
            rgb = np.asarray(image.convert('RGB'))
        height, width = rgb.shape[:2]
        truth = label_masks(dataset / sample['label'], width, height)
        predicted = [] if prediction.masks is None else [mask.cpu().numpy() > .5 for mask in prediction.masks.data]
        matrix = np.zeros((len(truth), len(predicted)))
        for i, gt in enumerate(truth):
            for j, pred in enumerate(predicted):
                matrix[i, j] = (gt & pred).sum() / max(1, (gt | pred).sum())
        pairs = list(zip(*linear_sum_assignment(1 - matrix))) if matrix.size else []
        tp = sum(matrix[i, j] >= .5 for i, j in pairs)
        gt_union = np.any(truth, axis=0) if truth else np.zeros((height, width), bool)
        pred_union = np.any(predicted, axis=0) if predicted else np.zeros((height, width), bool)
        union_iou = float((gt_union & pred_union).sum() / max(1, (gt_union | pred_union).sum()))
        record = {'sample': sample['key'], 'fid': sample['fid'], 'truth_instances': len(truth),
                  'predicted_instances': len(predicted), 'tp_iou50': int(tp),
                  'fp_iou50': len(predicted) - int(tp), 'fn_iou50': len(truth) - int(tp),
                  'union_mask_iou': union_iou,
                  'matched_ious': [float(matrix[i,j]) for i,j in pairs],
                  'confidences': prediction.boxes.conf.cpu().tolist(),
                  'predicted_segments_pixel': [] if prediction.masks is None else [p.tolist() for p in prediction.masks.xy]}
        # Each original Zhuangli polygon is evaluated once in its own window.
        # Neighbour labels appear in several windows and are reported separately above.
        target_root = manifest.get('zhuangli_raster')
        if target_root:
            target_path = (Path(target_root) / 'samples' /
                           sample['key'].removeprefix('zhuangli_') / 'polygon_mask.png')
            with Image.open(target_path) as image:
                target = np.asarray(image) > 0
            target_ious = [float((target & pred).sum() / max(1, (target | pred).sum())) for pred in predicted]
            record['original_polygon_best_iou'] = max(target_ious, default=0.)
            record['original_polygon_detected_iou50'] = record['original_polygon_best_iou'] >= .5
            record['original_polygon_mask'] = str(target_path)
        np.savez_compressed(output / (sample['key'] + '_masks.npz'),
                            predicted=np.asarray(predicted, dtype=bool).reshape(-1, height, width),
                            truth=np.asarray(truth, dtype=bool).reshape(-1, height, width))
        results.append(record)
        overlay = rgb.copy()
        for mask, color in [(gt_union, [45, 235, 135]), (pred_union, [245, 70, 210])]:
            contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(overlay, contours, -1, color, 1)
        scale = max(1, 400 // max(width, height))
        image = Image.fromarray(overlay).resize((width * scale, height * scale), Image.Resampling.NEAREST)
        preview = Image.new('RGB', (max(420, image.width), image.height + 70), '#151b24')
        preview.paste(image, ((preview.width - image.width) // 2, 65))
        draw = ImageDraw.Draw(preview)
        draw.text((12, 10), f"Zhuangli FID {sample['fid']} | TP {tp} FP {record['fp_iou50']} FN {record['fn_iou50']}", fill='white')
        draw.text((12, 28), f"Union mask IoU {union_iou:.3f} | confidence >= 0.25", fill='white')
        draw.text((12, 46), 'Green: truth | Magenta: prediction', fill='white')
        preview.save(output / (sample['key'] + '.png'))
        previews.append(preview)
    canvas = Image.new('RGB', (max(p.width for p in previews) * 3,
                                   max(p.height for p in previews) * 2), '#10151c')
    for i, preview in enumerate(previews):
        canvas.paste(preview, ((i % 3) * (canvas.width // 3), (i // 3) * (canvas.height // 2)))
    canvas.save(output / 'zhuangli_predictions.jpg', quality=95)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--weights', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', default='0')
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--imgsz', type=int, default=256)
    parser.add_argument('--batch', type=int, default=4)
    parser.add_argument('--evaluate-only', action='store_true',
                        help='Evaluate an existing trained checkpoint without training or selecting it')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    state_path = args.output / 'run_state.json'
    started = time.time()
    torch.set_num_threads(4)
    write_json(state_path, {'status': 'evaluating' if args.evaluate_only else 'training', 'started_at_epoch': started})
    try:
        model = YOLO(str(args.weights))
        if not args.evaluate_only:
            model.train(data=str(args.dataset / 'data.yaml'), epochs=args.epochs, patience=25,
                        imgsz=args.imgsz, batch=args.batch, device=args.device, workers=0,
                        project=str(args.output), name='training', exist_ok=False,
                        seed=42, deterministic=True, amp=False, cache=False,
                        hsv_h=0., hsv_s=0., hsv_v=.1, degrees=180., translate=.1, scale=.2,
                        fliplr=.5, flipud=.5, mosaic=.5, close_mosaic=10,
                        mask_ratio=1, overlap_mask=True, plots=False, save=True)
        best = args.weights if args.evaluate_only else Path(model.trainer.best)
        if not best.is_file():
            raise RuntimeError('training did not produce best.pt')
        write_json(state_path, {'status': 'evaluating', 'best': str(best)})
        trained = YOLO(str(best))
        metrics = trained.val(data=str(args.dataset / 'data.yaml'), split='test',
                              device=args.device, imgsz=args.imgsz, batch=args.batch, workers=0, mask_ratio=1,
                              conf=.001, iou=.7, plots=False, save_json=False,
                              project=str(args.output), name='zhuangli_validation')
        prediction_root = args.output / 'predictions'
        prediction_root.mkdir()
        windows = evaluate_windows(trained, args.dataset, prediction_root, args.device, args.imgsz)
        report = {'status': 'complete', 'model': str(best),
                  'model_sha256': hashlib.sha256(best.read_bytes()).hexdigest(),
                  'initial_weights_sha256': None if args.evaluate_only else hashlib.sha256(args.weights.read_bytes()).hexdigest(),
                  'evaluation_only': args.evaluate_only,
                  'ultralytics': ultralytics.__version__, 'torch': torch.__version__,
                  'device': args.device, 'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                  'peak_gpu_allocated_bytes': torch.cuda.max_memory_allocated() if torch.cuda.is_available() else 0,
                  'elapsed_seconds': time.time() - started,
                  'test_policy': 'Zhuangli held out from training and checkpoint selection',
                  'ultralytics_metrics': {k: float(v) for k,v in metrics.results_dict.items()},
                  'native_pixel_evaluation': {'confidence': .25, 'mask_iou_threshold': .5, 'windows': windows}}
        write_json(args.output / 'validation_report.json', report)
        write_json(state_path, {'status': 'complete', 'model': str(best), 'report': str(args.output / 'validation_report.json')})
        print(json.dumps(report, indent=2))
    except BaseException:
        write_json(state_path, {'status': 'failed', 'traceback': traceback.format_exc()})
        raise


if __name__ == '__main__':
    main()
