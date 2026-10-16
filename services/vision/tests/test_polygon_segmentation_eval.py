"""Empty predictions must be reported as misses, with a usable overlay."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image


@unittest.skipUnless(importlib.util.find_spec('ultralytics'), 'requires Vision runtime')
class PolygonEvaluationTests(unittest.TestCase):
    def test_empty_prediction_retains_false_negative(self):
        script = Path(__file__).resolve().parents[1] / 'train_polygon_segmentation.py'
        spec = importlib.util.spec_from_file_location('segmentation_eval', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new('RGB', (10, 10)).save(root / 'test.png')
            (root / 'test.txt').write_text('0 0.2 0.2 0.8 0.2 0.8 0.8 0.2 0.8\n')
            (root / 'dataset_manifest.json').write_text(json.dumps({'samples': [
                {'split': 'test', 'key': 'zhuangli_test', 'fid': 461,
                 'image': 'test.png', 'label': 'test.txt'}]}))
            empty = SimpleNamespace(masks=None, boxes=SimpleNamespace(conf=module.torch.tensor([])))
            model = SimpleNamespace(predict=lambda *a, **kw: [empty])
            output = root / 'predictions'
            output.mkdir()
            report = module.evaluate_windows(model, root, output, 'cpu', 256)
            self.assertEqual(report[0]['truth_instances'], 1)
            self.assertEqual(report[0]['predicted_instances'], 0)
            self.assertEqual(report[0]['fn_iou50'], 1)
            self.assertEqual(report[0]['union_mask_iou'], 0.)
            self.assertTrue((output / 'zhuangli_predictions.jpg').is_file())


if __name__ == '__main__':
    unittest.main()
