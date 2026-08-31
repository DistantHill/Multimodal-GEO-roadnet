from __future__ import annotations

import configparser
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "roadnet_tools"


class PluginPackageTests(unittest.TestCase):
    def test_required_qgis_plugin_files_exist(self):
        for relative_path in ("__init__.py", "metadata.txt", "plugin.py", "icon.svg"):
            with self.subTest(relative_path=relative_path):
                self.assertTrue((PLUGIN_ROOT / relative_path).is_file())

    def test_metadata_targets_qgis_344_and_is_not_experimental(self):
        metadata = configparser.ConfigParser()
        metadata.read(PLUGIN_ROOT / "metadata.txt", encoding="utf-8")
        general = metadata["general"]
        self.assertEqual(general["name"], "Roadnet Tools")
        self.assertEqual(general["qgisMinimumVersion"], "3.44")
        self.assertEqual(general.getboolean("experimental"), False)

    def test_primary_output_layer_is_named_tmp(self):
        source = (PLUGIN_ROOT / "layers.py").read_text(encoding="utf-8")
        self.assertIn('OUTPUT_GROUP_NAME = "tmp"', source)
        self.assertIn('OUTPUT_LAYER_NAME = "tmp"', source)
        self.assertNotIn("QgsVectorFileWriter", source)


if __name__ == "__main__":
    unittest.main()
