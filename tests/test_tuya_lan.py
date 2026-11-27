"""Production adapter transactions with an injected SDK peer; not hardware."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.lightcadence_engine.deployment import configured_runtime
from engine.lightcadence_engine.effects import LightTarget
from engine.lightcadence_engine.enrollment import ChromaProfile
from engine.lightcadence_engine.recovery import RecoveryJournal
from engine.lightcadence_engine.sessions import DeviceState, StopPolicy
from engine.lightcadence_engine.tuya_lan import (
    TuyaBinding, TuyaError, TuyaStatePort, _factory, compose_mixed_candidate,
    decode_state, project_state,
)


def binding():
    return TuyaBinding(ChromaProfile("tuya", "ha-tuya", "synthetic", "1",
        "127.0.0.2", 0., 6668, "tuya_v35"), "synthetic-only",
        "synthetic1234567", 20, 21, 24)


class SdkPeer:
    def __init__(self):
        self.dps = {"20": True, "21": "colour", "24": "00fa00000320"}
        self.writes = []
        self.connections = []
        self.fail = False
        self.apply = True

    def __call__(self, b, timeout):
        device = SdkDevice(self)
        self.connections.append(device)
        return device


class SdkDevice:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False
        self.retry_limit = 1
        self.cmd_retcode = None

    def status(self):
        assert not self.closed
        self.cmd_retcode = 0
        return {"dps": dict(self.peer.dps)}

    def set_socketRetryLimit(self, n):
        self.retry_limit = n

    def set_multiple_values(self, values, nowait=False):
        assert not self.closed and not nowait and self.retry_limit == 0
        self.peer.writes.append(dict(values))
        if self.peer.apply:
            self.peer.dps.update(values)
        self.cmd_retcode = 1 if self.peer.fail else 0
        return {"Err": "synthetic"} if self.peer.fail else None

    def close(self):
        self.closed = True


def start_body():
    return {"op": "start", "request_id": "start", "zone_id": "zone", "devices": ["tuya"],
            "effect": {"kind": "static", "color": [0, 1, 0], "brightness": .8}}


class TuyaTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_real_sdk_constructor_no_socket_or_retry(self):
        import tinytuya
        self.assertEqual(tinytuya.__version__, "1.21.0")
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            dev = _factory(binding(), .5)
            try:
                self.assertFalse(dev.auto_ip)
                self.assertEqual(dev.version, 3.5)
                self.assertTrue(dev.socketPersistent)
                self.assertEqual(dev.socketRetryLimit, 1)
                self.assertEqual(dev.sendRetryLimit, 0)
                self.assertFalse(dev.retry)
            finally:
                dev.close()

    async def test_exact_native_restore_and_coalescing(self):
        peer = SdkPeer()
        original = dict(peer.dps)
        runtime = compose_mixed_candidate([], [binding()], enabled=True, factory=peer)
        try:
            state = await runtime.command(start_body())
            await runtime.physical.output.drain()
            self.assertEqual(peer.dps["24"], "007803e80320")
            self.assertEqual(len(peer.connections), 2)
            self.assertEqual(len(peer.writes), 1)
            runtime.tick()
            await runtime.physical.output.drain()
            self.assertEqual(len(peer.writes), 1)
            session = state["sessions"][0]
            end = await runtime.command({"op": "stop", "session_id": session["id"],
                                         "revision": session["revision"]})
            self.assertEqual(end["sessions"][0]["phase"], "stopped")
            self.assertEqual(peer.dps, original)
            self.assertTrue(all(d.closed for d in peer.connections))
            self.assertIsNone(runtime.manager.owner("tuya"))
        finally:
            await runtime.close()

    async def test_invalid_mode_and_uncertain_output_no_reconnect(self):
        peer = SdkPeer()
        runtime = compose_mixed_candidate([], [binding()], enabled=True, factory=peer)
        try:
            peer.dps["21"] = "scene"
            with self.assertRaises(TuyaError):
                await runtime.command(start_body())
            self.assertEqual(peer.writes, [])
            self.assertEqual(runtime.manager.snapshots(), ())
            peer.dps["21"] = "colour"
            # The first request never acquired a lease, so a different ID is safe.
            peer.fail = True
            await runtime.command({**start_body(), "request_id": "next"})
            await runtime.physical.output.drain()
            writes, connections = len(peer.writes), len(peer.connections)
            for _ in range(3):
                runtime.tick()
                await runtime.physical.output.drain()
            self.assertEqual(len(peer.writes), writes)
            self.assertEqual(len(peer.connections), connections)
            self.assertEqual(writes, 1)
            self.assertIsNotNone(runtime.manager.owner("tuya"))
        finally:
            await runtime.close()

    async def test_manual_quantization_and_independent_readback(self):
        peer = SdkPeer()
        runtime = compose_mixed_candidate([], [binding()], enabled=True, factory=peer)
        try:
            await runtime.command(start_body())
            await runtime.physical.output.drain()
            requested = DeviceState(True, LightTarget(.7, .1, .3, .42))
            expected = project_state(requested)
            self.assertEqual(await runtime.physical.manual_set("tuya", requested), "confirmed")
            self.assertEqual(peer.dps["24"], expected.native_hsv)
            self.assertIsNone(runtime.manager.owner("tuya"))
            peer.apply = False
            self.assertEqual(await runtime.physical.manual_set("tuya",
                DeviceState(True, LightTarget(1, 0, 0, .6))), "unconfirmed")
            self.assertEqual(peer.dps["24"], expected.native_hsv)
            peer.apply = True
            self.assertEqual(await runtime.physical.manual_off("tuya"), "confirmed")
            self.assertFalse(peer.dps["20"])
            self.assertEqual(peer.dps["24"], expected.native_hsv)
            before = len(peer.writes)
            port = runtime.physical._adapters["tuya"].state_port
            self.assertFalse(await port.write(binding().profile,
                decode_state(False, "000003e80100")))
            self.assertEqual(len(peer.writes), before)
        finally:
            await runtime.close()

    async def test_journal_and_strict_owner_file(self):
        b = binding()
        with tempfile.TemporaryDirectory() as directory:
            j = RecoveryJournal(directory, {"tuya": "binding"})
            state = decode_state(False, "016800000000")
            j.add({"tuya": state}, StopPolicy.RESTORE)
            j.close()
            j = RecoveryJournal(directory, {"tuya": "binding"})
            try:
                record = j.snapshot()[0]
                self.assertEqual(record["original"]["native_hsv"], "016800000000")
                self.assertEqual(j.prepare_recovery("tuya", record["record_id"]), state)
                j.remove(["tuya"])
            finally:
                j.close()
            path = Path(directory) / "devices.json"
            env = {"LIGHTCADENCE_MODE": "physical_candidate", "LIGHTCADENCE_DEVICE_FILE": str(path),
                   "LIGHTCADENCE_JOURNAL_DIR": directory}
            item = {"ha_registry_id": "tuya", "model": "synthetic", "firmware": "1",
                    "host": "127.0.0.2", "interval": .1, "lan_id": b.lan_id, "transport": "tuya_v35",
                    "tuya": {"local_key": b.local_key, "power_dp": 20, "mode_dp": 21, "colour_dp": 24}}
            def save(devices):
                path.write_text(json.dumps({"schema_version": 1, "owner_hardware_test": True,
                    "devices": devices}))
            save([item])
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                runtime = configured_runtime(env)
                try:
                    serialized = json.dumps(runtime.state())
                    self.assertNotIn(b.local_key, serialized)
                    self.assertNotIn(b.lan_id, serialized)
                    self.assertEqual(runtime.state()["device_transports"]["tuya"]["kind"], "tuya_v35")
                finally:
                    await runtime.close()
            for changed in ({**item, "tuya": {**item["tuya"], "mode_dp": 20}},
                            {**item, "tuya": {**item["tuya"], "local_key": ""}},
                            {**item, "chroma_port": 55444}, {**item, "host": "8.8.8.8"}):
                save([changed])
                with self.assertRaises(ValueError):
                    configured_runtime(env)
