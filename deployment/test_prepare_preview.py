"""Original local build and offline activation regression tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare_preview import REQUIRED, INTEGRATION, MANIFEST, activate, prepare, seal, verify


class PreviewDeploymentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "ha"
        (self.config / "custom_components").mkdir(parents=True)
        self.other = self.config / "configuration.yaml"
        self.other.write_text("keep me")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.a = self.bundle("a")
        self.b = self.bundle("b")
        self.target = self.config / INTEGRATION

    def bundle(self, letter):
        root = self.root / letter
        for name in REQUIRED:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(letter + name)
        seal(root, letter * 40, "synthetic-node", "synthetic-npm")
        return root

    def test_integrity_rejects_extra_missing_changed_and_duplicate_fields(self):
        self.assertEqual(verify(self.a)["source_sha"], "a" * 40)
        extra = self.a / "unexpected"
        extra.write_text("extra")
        with self.assertRaises(ValueError):
            verify(self.a)
        extra.unlink()
        panel = self.a / INTEGRATION / "frontend/panel.js"
        old = panel.read_bytes()
        panel.unlink()
        with self.assertRaises(ValueError):
            verify(self.a)
        panel.write_bytes(old + b"x")
        with self.assertRaises(ValueError):
            verify(self.a)
        panel.write_bytes(old)
        manifest = self.a / MANIFEST
        manifest.write_text(manifest.read_text().replace('  "mode": "preview",',
            '  "mode": "preview", "mode": "preview",'))
        with self.assertRaises(ValueError):
            verify(self.a)

    def test_install_update_rollback_and_preserve_other_data(self):
        with self.assertRaises(ValueError):
            activate(self.a, self.config)
        self.assertFalse(self.target.exists())
        first = activate(self.a, self.config, ha_stopped=True)
        self.assertTrue(first["changed"])
        self.assertFalse(activate(self.a, self.config, ha_stopped=True)["changed"])
        cache = self.target / "__pycache__"
        cache.mkdir()
        (cache / "runtime.pyc").write_bytes(b"cache")
        with self.assertRaises(ValueError):
            activate(self.b, self.config, ha_stopped=True)
        update = activate(self.b, self.config, ha_stopped=True,
                          previous=self.a, backup_dir=self.backups)
        self.assertTrue(Path(update["backup"]).joinpath("__pycache__/runtime.pyc").exists())
        self.assertEqual((self.target / "frontend/panel.js").read_bytes(),
                         (self.b / INTEGRATION / "frontend/panel.js").read_bytes())
        rollback = activate(self.a, self.config, ha_stopped=True,
                            previous=self.b, backup_dir=self.backups)
        self.assertTrue(Path(rollback["backup"]).is_dir())
        self.assertEqual((self.target / "frontend/panel.js").read_bytes(),
                         (self.a / INTEGRATION / "frontend/panel.js").read_bytes())
        self.assertEqual(self.other.read_text(), "keep me")

    def test_modified_installed_code_and_symlink_refused(self):
        activate(self.a, self.config, ha_stopped=True)
        panel = self.target / "frontend/panel.js"
        panel.write_text("owner modification")
        with self.assertRaises(ValueError):
            activate(self.b, self.config, ha_stopped=True,
                     previous=self.a, backup_dir=self.backups)
        self.assertEqual(panel.read_text(), "owner modification")
        self.assertFalse(list(self.backups.iterdir()))
        (self.b / "link").symlink_to(self.other)
        with self.assertRaises(ValueError):
            verify(self.b)

    def test_failed_swap_restores_previous_directory(self):
        activate(self.a, self.config, ha_stopped=True)
        original = os.rename
        def fail_once(src, dst):
            if ".lightcadence-stage-" in str(src):
                raise OSError("synthetic rename failure")
            return original(src, dst)
        with patch("prepare_preview.os.rename", side_effect=fail_once):
            with self.assertRaises(OSError):
                activate(self.b, self.config, ha_stopped=True,
                         previous=self.a, backup_dir=self.backups)
        self.assertEqual((self.target / "frontend/panel.js").read_bytes(),
                         (self.a / INTEGRATION / "frontend/panel.js").read_bytes())
        self.assertFalse(list(self.backups.iterdir()))

    def test_explicit_hardware_overlay_is_sealed_and_never_implicit(self):
        overlay = self.a / "compose.hardware-test.yaml"
        overlay.write_text("synthetic hardware overlay")
        with self.assertRaises(ValueError):
            verify(self.a)
        (self.a / MANIFEST).unlink()
        with self.assertRaises(ValueError):
            seal(self.a, "a"*40, "node", "npm")
        seal(self.a, "a"*40, "node", "npm", hardware_candidate=True)
        self.assertEqual(verify(self.a)["mode"], "hardware_candidate")
        overlay.unlink()
        with self.assertRaises(ValueError):
            verify(self.a)
        self.assertEqual(verify(self.b)["mode"], "preview")

    def test_prepare_rejects_non_full_commit_before_build(self):
        with self.assertRaises(ValueError):
            prepare(self.root, "main", self.root / "new")
        self.assertFalse((self.root / "new").exists())
