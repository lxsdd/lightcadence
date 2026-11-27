"""Offline tests for the owner-local deployment boundary and real API admission."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lightcadence_engine.deployment import configured_runtime
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
