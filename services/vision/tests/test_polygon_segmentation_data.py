"""Dataset label and split semantics, runnable in the Pipeline environment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from shapely.geometry import Polygon, box

MODULE = Path(__file__).resolve().parents[1] / 'prepare_polygon_segmentation.py'
spec = importlib.util.spec_from_file_location('polygon_seg_data', MODULE)
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)


class PolygonSegmentationDataTests(unittest.TestCase):
    def test_overlap_components_are_transitive(self):
        samples = [{'window': box(0, 0, 10, 10)}, {'window': box(9, 0, 19, 10)},
                   {'window': box(18, 0, 28, 10)}, {'window': box(100, 100, 110, 110)}]
        groups = data.connected_windows(samples)
        self.assertEqual(sorted(sorted(g) for g in groups), [[0, 1, 2], [3]])

    def test_only_unique_tp_polygons_are_foreground(self):
        geometry = box(0, 0, 10, 10)
        samples = [{'label': 'TP', 'projected': geometry},
                   {'label': 'TP', 'projected': geometry.reverse()},
                   {'label': 'FP', 'projected': box(30, 30, 40, 40)}]
        self.assertEqual(len(data.unique_foreground(samples)), 1)

    def sample(self, root):
        source = root / 'source'
        source.mkdir()
        Image.new('L', (10, 10), 42).save(source / 'gray.png')
        return {'key': 'FP_test', 'label': 'FP', 'source_file': 'FP_test.gpkg', 'fid': 7,
                'grid': {'west': 0, 'south': 0, 'east': 100, 'north': 100},
                'window': box(0, 0, 100, 100), 'input_directory': source}

    def test_fp_stays_negative_without_nearby_tp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = data.export_sample(self.sample(root), [], root / 'dataset', 'train')
            self.assertEqual(row['objects'], 0)
            self.assertEqual((root / 'dataset' / row['label']).read_text(), '')
            with Image.open(root / 'dataset' / row['image']) as image:
                self.assertEqual(image.mode, 'RGB')
                self.assertEqual(image.getpixel((0, 0)), (42, 42, 42))

    def test_nearby_tp_is_labelled_and_clipped_even_in_fp_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = data.export_sample(self.sample(root), [box(80, 30, 120, 60)], root / 'dataset', 'train')
            self.assertEqual(row['objects'], 1)
            values = [float(v) for v in (root / 'dataset' / row['label']).read_text().split()]
            self.assertEqual(values[0], 0)
            self.assertTrue(all(0 <= value <= 1 for value in values[1:]))
            self.assertEqual(max(values[1::2]), 1)

    def test_holes_are_not_silently_filled_by_yolo_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            polygon = Polygon([(10, 10), (90, 10), (90, 90), (10, 90)],
                              [[(20, 20), (30, 20), (30, 30), (20, 30)]])
            with self.assertRaisesRegex(ValueError, 'holes'):
                data.export_sample(self.sample(root), [polygon], root / 'dataset', 'train')


if __name__ == '__main__':
    unittest.main()
