import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


@unittest.skipUnless(importlib.util.find_spec("astrbot") is not None, "Requires AstrBot SDK")
class InstallationTests(unittest.TestCase):
    def test_fresh_install_and_hot_reload_preserve_other_plugin_data(self):
        source = Path(__file__).resolve().parents[1]
        probe = source / "tests/installation_probe.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "ASTRBOT_ROOT": directory}
            result = subprocess.run(
                [sys.executable, str(probe), directory, str(source)],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            report = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(report["installed"])
            self.assertTrue(report["configured"])
            self.assertTrue(report["hot_reloaded"])
            self.assertTrue(report["other_plugin_data_unchanged"])
            self.assertEqual(set(report["databases"]), {"mood.sqlite3"})

    def test_zip_upgrade_keeps_existing_mood_and_configuration(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "mood.zip"
            with ZipFile(archive, "w") as bundle:
                for file in (*source.glob("*.py"), source / "metadata.yaml", source / "_conf_schema.json", source / "README.md"):
                    bundle.write(file, source.name + "/" + file.name)
            result = subprocess.run(
                [sys.executable, str(source / "tests/installation_probe.py"), str(root), str(source), str(archive)],
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "ASTRBOT_ROOT": str(root)},
                capture_output=True, text=True, encoding="utf-8", timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-5000:] + result.stderr[-5000:])
            report = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(report["archive_upgrade"])
            self.assertTrue(report["hot_reloaded"])
