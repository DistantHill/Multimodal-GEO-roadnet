"""Exercise export failure cleanup without loading GPU or video dependencies."""

import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


class TrackingExportTests(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        self.cv2 = Mock()
        self.capture = self.cv2.VideoCapture.return_value
        self.capture.isOpened.return_value = True
        self.capture.read.return_value = (False, None)
        self.capture.get.side_effect = [25.0, 640, 480]
        self.writer = self.cv2.VideoWriter.return_value
        self.writer.isOpened.return_value = True
        self.ultralytics = Mock()
        path = Path(__file__).resolve().parents[1] / "track_export.py"
        spec = importlib.util.spec_from_file_location("vision_track_export_test", path)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "cv2": self.cv2, "numpy": Mock(), "ultralytics": self.ultralytics,
        }):
            spec.loader.exec_module(self.module)
        self.module.parse_args = Mock(return_value=argparse.Namespace(
            source="sample.mp4", output=self.output.name, model="local.pt",
            tracker="tracker.yaml", device="cpu", imgsz=640, conf=0.1,
            point="bottom_center",
        ))

    def test_releases_capture_when_input_video_cannot_open(self):
        self.capture.isOpened.return_value = False
        with self.assertRaisesRegex(SystemExit, "Cannot open video"):
            self.module.main()
        self.capture.release.assert_called_once()
        self.cv2.VideoWriter.assert_not_called()

    def test_releases_capture_when_output_video_cannot_open(self):
        self.writer.isOpened.return_value = False
        with self.assertRaisesRegex(SystemExit, "Cannot create output video"):
            self.module.main()
        self.capture.release.assert_called_once()
        self.writer.release.assert_called_once()

    def test_releases_resources_when_model_load_fails(self):
        self.ultralytics.YOLO.side_effect = RuntimeError("model load failed")
        with self.assertRaisesRegex(RuntimeError, "model load failed"):
            self.module.main()
        self.capture.release.assert_called_once()
        self.writer.release.assert_called_once()

    def test_releases_resources_and_closes_csv_when_inference_fails(self):
        self.capture.read.return_value = (True, object())
        self.ultralytics.YOLO.return_value.track.side_effect = RuntimeError("inference failed")
        with self.assertRaisesRegex(RuntimeError, "inference failed"):
            self.module.main()
        self.capture.release.assert_called_once()
        self.writer.release.assert_called_once()
        self.assertTrue((Path(self.output.name) / "sample_tracks.csv").read_text().startswith("frame,"))

    def test_invalid_fps_uses_finite_positive_fallback(self):
        for fps in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(fps=fps), patch("builtins.print"):
                self.capture.get.side_effect = [fps, 640, 480]
                self.module.main()
                self.assertEqual(self.cv2.VideoWriter.call_args.args[2], 30.0)

    def test_valid_fps_is_preserved_and_resources_are_released(self):
        with patch("builtins.print"):
            self.module.main()
        self.assertEqual(self.cv2.VideoWriter.call_args.args[2], 25.0)
        self.capture.release.assert_called_once()
        self.writer.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
