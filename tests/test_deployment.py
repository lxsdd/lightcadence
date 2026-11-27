"""Offline tests for the owner-local deployment boundary and real API admission."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lightcadence_engine.deployment import configured_runtime
from engine.lightcadence_engine.server import check_configuration
from engine.lightcadence_engine.runtime import PhysicalRuntime, PreviewRuntime


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "devices.json"
        self.env = {"LIGHTCADENCE_MODE": "physical_candidate", "LIGHTCADENCE_DEVICE_FILE": str(self.path), "LIGHTCADENCE_JOURNAL_DIR": tmp.name}
        self.device = {"ha_registry_id": "synthetic", "model": "synthetic", "firmware": "1",
                       "host": "127.0.0.1", "interval": .02, "lan_id": "0x1234"}
        self.document = {"schema_version": 1, "owner_hardware_test": True, "devices": [self.device]}

    def save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    async def test_default_preview_and_mode_mismatch(self):
        self.assertIs(type(configured_runtime({})), PreviewRuntime)
        for env in ({"LIGHTCADENCE_DEVICE_FILE": str(self.path)}, {"LIGHTCADENCE_MODE": "physical_candidate"},
                    {"LIGHTCADENCE_MODE": "typo"}):
            with self.assertRaises(ValueError):
                configured_runtime(env)

    async def test_explicit_binding_constructs_without_socket(self):
        self.save(self.document)
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            runtime = configured_runtime(self.env)
            self.assertIsInstance(runtime, PhysicalRuntime)
            self.assertEqual(runtime.state()["enrolled_devices"], ("synthetic",))
            self.assertFalse(runtime.state()["hardware_qualified"])
            await runtime.close()

    async def test_explicit_tcp_profile_requires_documented_cadence(self):
        self.save({**self.document, "devices": [{**self.device, "transport": "yeelight_tcp", "interval": 1.0}]})
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            runtime = configured_runtime(self.env)
            self.assertEqual(runtime.state()["device_transports"]["synthetic"], {"kind": "yeelight_tcp", "interval_seconds": 1.0})
            await runtime.close()
        for changes in ({"transport": "yeelight_tcp", "interval": .02}, {"transport": "unknown"},
                        {"transport": "yeelight_tcp", "interval": 1., "chroma_port": 55444}, {"host": 2130706433}):
            self.save({**self.document, "devices": [{**self.device, **changes}]})
            with self.assertRaises(ValueError):
                configured_runtime(self.env)

    async def test_white_profile_explicit_range_and_transport(self):
        white = {**self.device, "transport": "yeelight_tcp", "interval": 1.,
                 "white_kelvin": 4000, "ct_min_kelvin": 2700, "ct_max_kelvin": 6500}
        self.save({**self.document, "devices": [white]})
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            runtime = configured_runtime(self.env)
            self.assertEqual(runtime.state()["device_transports"]["synthetic"]["white_kelvin"], 4000)
            await runtime.close()
        for changes in ({"white_kelvin": 2000}, {"ct_min_kelvin": None},
                        {"white_kelvin": True}, {"transport": "chroma_v2"}):
            self.save({**self.document, "devices": [{**white, **changes}]})
            with self.assertRaises(ValueError):
                configured_runtime(self.env)

    async def test_dual_mode_explicit_range_and_no_network(self):
        for transport, interval in (("chroma_v2", .02), ("yeelight_tcp", 1.)):
            dual = {**self.device, "transport": transport, "interval": interval,
                    "ct_min_kelvin": 2700, "ct_max_kelvin": 6500}
            self.save({**self.document, "devices": [dual]})
            with patch("socket.socket", side_effect=AssertionError("unexpected network")):
                runtime = configured_runtime(self.env)
                self.assertEqual(runtime.state()["device_transports"]["synthetic"],
                    {"kind": transport, "interval_seconds": interval, "dual_mode": True,
                     "ct_min_kelvin": 2700, "ct_max_kelvin": 6500})
                await runtime.close()
            for changes in ({"ct_min_kelvin": None}, {"ct_min_kelvin": True},
                            {"ct_min_kelvin": 7000}, {"ct_max_kelvin": 9000}):
                self.save({**self.document, "devices": [{**dual, **changes}]})
                with self.assertRaises(ValueError):
                    configured_runtime(self.env)

    async def test_invalid_local_documents_fail_closed(self):
        for doc in ({**self.document, "owner_hardware_test": False},
                    {**self.document, "schema_version": True},
                    {**self.document, "devices": []},
                    {**self.document, "devices": [self.device, self.device]},
                    {**self.document, "devices": [{**self.device, "host": "8.8.8.8"}]},
                    {**self.document, "devices": [{**self.device, "unexpected": True}]}):
            self.save(doc)
            with self.assertRaisesRegex(ValueError, "invalid owner-local"):
                configured_runtime(self.env)
        self.path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            configured_runtime(self.env)

    async def test_read_only_preflight_for_physical_calibration(self):
        root = self.path.parent
        token = root / "token"
        token.write_text("synthetic_" + "a" * 32)
        library = root / "library"
        library.mkdir()
        self.save({**self.document, "devices": [{**self.device,
            "transport": "yeelight_tcp", "interval": 1.,
            "ct_min_kelvin": 2700, "ct_max_kelvin": 6500}]})
        env = {**self.env, "LIGHTCADENCE_TOKEN_FILE": str(token),
               "LIGHTCADENCE_LIBRARY_DIR": str(library)}
        def snapshot():
            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        before = snapshot()
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            result = check_configuration(env)
        self.assertEqual(result, {"mode": "physical_candidate", "persistent_library": True,
            "devices": 1, "native_kelvin_devices": 1, "recovery_records": 0,
            "unmatched_recovery_records": 0, "hardware_qualified": False})
        self.assertEqual(snapshot(), before)
        self.assertFalse((root / "recovery.lock").exists())
        self.assertFalse((library / "library.lock").exists())
        # The original startup accepts the very same document.
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            runtime = configured_runtime(env)
            await runtime.close()
        before = snapshot()
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            self.assertEqual(check_configuration(env), result)
        self.assertEqual(snapshot(), before)

    async def test_preflight_invalid_input_and_pending_recovery(self):
        root = self.path.parent
        token = root / "token"
        token.write_text("synthetic_" + "a" * 32)
        env = {**self.env, "LIGHTCADENCE_TOKEN_FILE": str(token)}
        self.save(self.document)
        for changes in ({"LIGHTCADENCE_PORT": "0"}, {"LIGHTCADENCE_LIBRARY_DIR": str(root)},
                        {"LIGHTCADENCE_MODE": "preview"}, {"LIGHTCADENCE_DEVICE_FILE": str(root)}):
            with self.assertRaises(ValueError):
                check_configuration({**env, **changes})
        self.save({**self.document, "devices": [self.device, self.device]})
        with self.assertRaises(ValueError):
            check_configuration(env)
        self.save(self.document)
        journal = root / "recovery-v1.json"
        record = {"id": "synthetic-record", "binding": "old-binding",
                  "original": {"power": True, "target": {"red": 1, "green": 0, "blue": 0, "brightness": .5}},
                  "policy": "restore"}
        journal.write_text(json.dumps({"schema_version": 1, "records": {"synthetic": record}}))
        before = journal.read_bytes()
        with patch("socket.socket", side_effect=AssertionError("unexpected network")):
            result = check_configuration(env)
        self.assertEqual(result["recovery_records"], 1)
        self.assertEqual(result["unmatched_recovery_records"], 1)
        self.assertNotIn("synthetic-record", json.dumps(result))
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse((root / "recovery.lock").exists())
        journal.write_text("invalid json")
        with self.assertRaises(ValueError):
            check_configuration(env)
        token.write_text("too-short")
        with self.assertRaises(ValueError):
            check_configuration(env)
        self.assertFalse((root / "recovery.lock").exists())

    async def test_preview_preflight_rejects_corrupt_library_without_mutation(self):
        root = self.path.parent
        token = root / "token"
        token.write_text("synthetic_" + "a" * 32)
        env = {"LIGHTCADENCE_TOKEN_FILE": str(token), "LIGHTCADENCE_LIBRARY_DIR": str(root)}
        library = root / "library-v1.json"
        old = b'{"schema_version":1,"revision":0,"zones":[],"scenes":[]}'
        library.write_bytes(old)
        result = check_configuration(env)
        self.assertEqual(result["mode"], "preview")
        self.assertTrue(result["persistent_library"])
        self.assertEqual(library.read_bytes(), old)
        library.write_text('{"schema_version":2}')
        with self.assertRaises(ValueError):
            check_configuration(env)
        self.assertFalse((root / "library.lock").exists())
