"""Actual original LAN state port and candidate composition, loopback peers only."""
import asyncio
import json
import unittest

from engine.lightcadence_engine.enrollment import ChromaProfile
from engine.lightcadence_engine.yeelight_lan import (
    LanBinding, LanStateError, YeelightLanStatePort, compose_lan_candidate,
    _decode, YeelightTcpOutput,
)


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, fixture):
        self.fixture = fixture

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        f = self.fixture
        f.probes.append(data)
        headers = ["HTTP/1.1 200 OK", f"id: {f.lan_id}", "model: synthetic",
                   "fw_ver: 1", f"Location: yeelight://127.0.0.1:{f.tcp_port}",
                   "support: get_prop set_scene set_power set_ct_abx"]
        if f.duplicate:
            headers.append("ID: 0xdead")
        self.transport.sendto(("\r\n".join(headers) + "\r\n\r\n").encode(), addr)


class LanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lan_id = "0x1234"
        self.duplicate = False
        self.probes = []
        self.commands = []
        self.properties = ["on", "60", "16711680", "1", "0", "0"]
        self.wrong_id = False
        self.reject = False
        self.apply = True
        self.connections = 0
        self.handlers = set()
        self.tcp = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.tcp_port = self.tcp.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        self.discovery, _ = await loop.create_datagram_endpoint(
            lambda: Discovery(self), local_addr=("127.0.0.1", 0))
        self.chroma, _ = await loop.create_datagram_endpoint(
            lambda: ChromaPeer(self), local_addr=("127.0.0.1", 0))
        self.chroma_messages = []
        self.profile = ChromaProfile("test", "ha-test", "synthetic", "1",
                                     "127.0.0.1", .02,
                                     self.chroma.get_extra_info("sockname")[1])
        self.binding = LanBinding(self.profile, "0x1234", self.tcp_port,
                                  self.discovery.get_extra_info("sockname")[1])
        self.port = YeelightLanStatePort([self.binding], timeout=.5)

    async def asyncTearDown(self):
        self.discovery.close()
        self.chroma.close()
        self.tcp.close()
        await self.tcp.wait_closed()
        if self.handlers:
            await asyncio.gather(*tuple(self.handlers), return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.connections += 1
        try:
            while line := await reader.readline():
                obj = json.loads(line)
                method = obj["method"]
                self.commands.append((method, obj["params"]))
                # Deliberately interleave unsolicited props with the actual response.
                writer.write(b'{"method":"props","params":{"power":"off"}}\r\n')
                rid = obj["id"] + int(self.wrong_id)
                if method == "get_prop":
                    values = self.properties.copy()
                    if obj["params"] == ["power", "bright", "rgb", "ct", "color_mode", "flowing", "music_on"]:
                        power, bright, color, mode, flowing, music = values
                        values = [power, bright, color if mode == "1" else "",
                                  color if mode == "2" else "", mode, flowing, music]
                    response = {"id": rid, "result": values}
                elif self.reject:
                    response = {"id": rid, "error": {"code": -1}}
                else:
                    if self.apply:
                        if method == "set_scene":
                            kind, color, bright = obj["params"]
                            self.properties[:4] = ["on", str(bright), str(color), "2" if kind == "ct" else "1"]
                        elif method == "set_power":
                            self.properties[0] = obj["params"][0]
                    response = {"id": rid, "result": ["ok"]}
                writer.write(json.dumps(response).encode() + b"\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    def tcp_binding(self):
        p = ChromaProfile("tcp-test", "tcp-ha-test", "synthetic", "1", "127.0.0.1",
                          1.0, self.tcp_port, "yeelight_tcp")
        return LanBinding(p, self.lan_id, self.tcp_port,
                          self.discovery.get_extra_info("sockname")[1])

    def ct_binding(self):
        p = ChromaProfile("white", "ha-white", "synthetic", "1", "127.0.0.1",
                          1., self.tcp_port, "yeelight_tcp", 4000, 2700, 6500)
        return LanBinding(p, self.lan_id, self.tcp_port,
                          self.discovery.get_extra_info("sockname")[1])

    def dual_binding(self):
        p = ChromaProfile("dual", "ha-dual", "synthetic", "1", "127.0.0.1",
                          1., self.tcp_port, "yeelight_tcp", None, 2700, 6500)
        return LanBinding(p, self.lan_id, self.tcp_port,
                          self.discovery.get_extra_info("sockname")[1])

    async def test_dual_mode_rgb_show_restores_native_ct_and_off(self):
        from engine.lightcadence_engine.sessions import DeviceState
        from engine.lightcadence_engine.effects import LightTarget
        for power in ("on", "off"):
            self.properties = [power, "60", "3000", "2", "0", "0"]
            runtime = compose_lan_candidate([self.dual_binding()], enabled=True, timeout=.5)
            try:
                start = await runtime.command({"op": "start", "request_id": "dual", "zone_id": "zone",
                    "devices": ["dual"], "effect": {"kind": "static", "color": [0,0,1], "brightness": .8}})
                await runtime.physical.output.drain()
                self.assertEqual(self.properties[:4], ["on", "80", "255", "1"])
                session = start["sessions"][0]
                end = await runtime.command({"op": "stop", "session_id": session["id"],
                                             "revision": session["revision"]})
                self.assertEqual(end["sessions"][0]["phase"], "stopped")
                self.assertEqual(self.properties[:4], [power, "60", "3000", "2"])
                self.assertIsNone(runtime.manager.owner("dual"))
                self.assertEqual(await runtime.physical.manual_set("dual",
                    DeviceState(True, LightTarget(0,1,0,.5))), "confirmed")
                self.assertEqual(self.properties[:4], ["on", "50", "65280", "1"])
                self.assertEqual(await runtime.physical.manual_set("dual",
                    DeviceState(True, LightTarget(1,1,1,.4), 3500)), "confirmed")
                self.assertEqual(self.properties[:4], ["on", "40", "3500", "2"])
            finally:
                await runtime.close()

    async def test_dual_mode_invalid_state_and_unconfirmed_restore(self):
        runtime = compose_lan_candidate([self.dual_binding()], enabled=True, timeout=.5)
        command = {"op": "start", "request_id": "dual", "zone_id": "zone",
                   "devices": ["dual"], "effect": {"kind": "static", "color": [0,0,1]}}
        try:
            for color, mode, flowing in [("2000", "2", "0"), ("", "2", "0"),
                                         ("120", "3", "0"), ("3000", "2", "1")]:
                self.properties = ["on", "60", color, mode, flowing, "0"]
                with self.assertRaises(LanStateError):
                    await runtime.command(command)
                self.assertEqual(runtime.manager.snapshots(), ())
            self.assertTrue(all(m == "get_prop" for m, _ in self.commands))
            self.properties = ["on", "60", "3000", "2", "0", "0"]
            start = await runtime.command(command)
            await runtime.physical.output.drain()
            s = start["sessions"][0]
            self.apply = False
            stop = {"op": "stop", "session_id": s["id"], "revision": s["revision"]}
            pending = await runtime.command(stop)
            self.assertEqual(pending["sessions"][0]["phase"], "stopping")
            self.assertEqual(runtime.manager.owner("dual"), s["id"])
            self.apply = True
            done = await runtime.command({**stop, "revision": pending["sessions"][0]["revision"]})
            self.assertEqual(done["sessions"][0]["phase"], "stopped")
            self.assertEqual(self.properties[:4], ["on", "60", "3000", "2"])
        finally:
            await runtime.close()

    async def test_explicit_kelvin_frame_uses_native_tcp_and_range(self):
        from types import SimpleNamespace
        from engine.lightcadence_engine.effects import KelvinTarget
        binding = self.dual_binding()
        authority = YeelightLanStatePort([binding], timeout=.5)
        adapter = YeelightTcpOutput(binding.profile, authority, lambda: 0.)
        try:
            await adapter.acquire()
            with self.assertRaises(LanStateError):
                await adapter.send_frame(SimpleNamespace(device_id="dual", target=KelvinTarget(1,1,1,.5,2000)))
            await adapter.send_frame(SimpleNamespace(device_id="dual", target=KelvinTarget(1,1,1,.5,3500)))
            self.assertEqual(self.properties[:4], ["on","50","3500","2"])
            self.assertIn(("set_scene", ["ct",3500,50]), self.commands)
        finally: adapter.close()

    async def test_white_output_and_exact_ct_restore(self):
        self.properties = ["on", "60", "3000", "2", "0", "0"]
        runtime = compose_lan_candidate([self.ct_binding()], enabled=True, timeout=.5)
        try:
            result = await runtime.command({"op": "start", "request_id": "white", "zone_id": "zone",
                "devices": ["white"], "effect": {"kind": "static", "color": [1,1,1], "brightness": .8}})
            await runtime.physical.output.drain()
            self.assertEqual(self.properties[:4], ["on", "80", "4000", "2"])
            self.assertEqual(self.chroma_messages, [])
            self.assertIn(("set_scene", ["ct", 4000, 80]), self.commands)
            session = result["sessions"][0]
            end = await runtime.command({"op": "stop", "session_id": session["id"],
                                         "revision": session["revision"]})
            self.assertEqual(end["sessions"][0]["phase"], "stopped")
            self.assertEqual(self.properties[:4], ["on", "60", "3000", "2"])
            self.assertIsNone(runtime.manager.owner("white"))
        finally:
            await runtime.close()

    async def test_ct_admission_and_manual_validation_before_detach(self):
        from engine.lightcadence_engine.sessions import SessionError, DeviceState
        from engine.lightcadence_engine.effects import LightTarget
        runtime = compose_lan_candidate([self.ct_binding()], enabled=True, timeout=.5)
        command = {"op": "start", "request_id": "white", "zone_id": "zone",
                   "devices": ["white"], "effect": {"kind": "static", "color": [1,1,1]}}
        try:
            self.properties = ["on", "60", "2000", "2", "0", "0"]
            with self.assertRaises(LanStateError):
                await runtime.command(command)
            self.assertEqual(runtime.manager.snapshots(), ())
            self.properties[2] = "3000"
            result = await runtime.command(command)
            await runtime.physical.output.drain()
            owner = result["sessions"][0]["id"]
            before = len(self.commands)
            for invalid in (DeviceState(True, LightTarget(1,0,0,.5)),
                            DeviceState(True, LightTarget(1,1,1,.5), 2000)):
                with self.assertRaises(SessionError):
                    await runtime.physical.manual_set("white", invalid)
            self.assertEqual(runtime.manager.owner("white"), owner)
            self.assertEqual(len(self.commands), before)
            self.assertEqual(await runtime.physical.manual_set("white",
                DeviceState(True, LightTarget(1,1,1,.5), 3500)), "confirmed")
            self.assertIsNone(runtime.manager.owner("white"))
            self.assertEqual(self.properties[:4], ["on", "50", "3500", "2"])
            self.assertEqual(await runtime.physical.manual_off("white"), "confirmed")
            self.assertEqual(self.properties[:4], ["off", "50", "3500", "2"])
        finally:
            await runtime.close()

    async def test_ct_recovery_journal_and_legacy_rgb(self):
        import tempfile
        from engine.lightcadence_engine.recovery import RecoveryJournal
        from engine.lightcadence_engine.sessions import DeviceState, StopPolicy
        from engine.lightcadence_engine.effects import LightTarget
        with tempfile.TemporaryDirectory() as directory:
            keys = {"white": "ct", "rgb": "rgb"}
            states = {"white": DeviceState(True, LightTarget(1,1,1,.6), 3000),
                      "rgb": _decode("off", 40, 255)}
            journal = RecoveryJournal(directory, keys)
            journal.add(states, StopPolicy.RESTORE)
            journal.close()
            journal = RecoveryJournal(directory, keys)
            try:
                snapshot = {r["device_id"]: r for r in journal.snapshot()}
                self.assertEqual(snapshot["white"]["original"]["kelvin"], 3000)
                self.assertNotIn("kelvin", snapshot["rgb"]["original"])
                for device, state in states.items():
                    self.assertEqual(journal.prepare_recovery(device, snapshot[device]["record_id"]), state)
            finally:
                journal.close()

    async def test_mixed_tuya_and_real_loopback_yeelight(self):
        from test_tuya_lan import binding, SdkPeer
        from engine.lightcadence_engine.tuya_lan import compose_mixed_candidate
        peer = SdkPeer()
        original = dict(peer.dps)
        runtime = compose_mixed_candidate([self.tcp_binding()], [binding()],
                                          enabled=True, timeout=.5, factory=peer)
        try:
            state = await runtime.command({"op": "start", "request_id": "mixed", "zone_id": "mixed",
                "devices": ["tcp-test", "tuya"], "effect": {"kind": "static", "color": [0,1,0]}})
            await runtime.physical.output.drain()
            self.assertEqual(self.properties[2], "65280")
            self.assertEqual(peer.dps["24"][:8], "007803e8")
            session = state["sessions"][0]
            # A manually detached Tuya device stays independent at mixed stop.
            self.assertEqual(await runtime.physical.manual_off("tuya"), "confirmed")
            self.assertIsNone(runtime.manager.owner("tuya"))
            current = runtime.manager.snapshot(session["id"])
            result = await runtime.command({"op": "stop", "session_id": session["id"], "revision": current.revision})
            self.assertEqual(result["sessions"][0]["phase"], "stopped")
            self.assertEqual(self.properties[2], "16711680")
            self.assertFalse(peer.dps["20"])
            self.assertNotEqual(peer.dps, original)
        finally:
            await runtime.close()

    async def test_normal_tcp_original_runtime_restore_and_no_udp(self):
        runtime = compose_lan_candidate([self.tcp_binding()], enabled=True, timeout=.5)
        try:
            result = await runtime.command({"op": "start", "request_id": "tcp", "zone_id": "zone",
                "devices": ["tcp-test"], "effect": {"kind": "static", "color": [0,0,1], "brightness": .8}})
            await runtime.physical.output.drain()
            self.assertEqual(self.properties[2], "255")
            self.assertEqual(self.chroma_messages, [])
            self.assertEqual(runtime.state()["device_transports"]["tcp-test"]["kind"], "yeelight_tcp")
            self.assertEqual(self.connections, 2)  # snapshot and persistent output
            session = result["sessions"][0]
            end = await runtime.command({"op": "stop", "session_id": session["id"],
                                         "revision": session["revision"]})
            self.assertEqual(end["sessions"][0]["phase"], "stopped")
            self.assertEqual(self.properties[2], "16711680")
            self.assertIsNone(runtime.manager.owner("tcp-test"))
        finally:
            await runtime.close()

    async def test_tcp_failure_does_not_reconnect_or_replay(self):
        binding = self.tcp_binding()
        authority = YeelightLanStatePort([binding], timeout=.5)
        adapter = YeelightTcpOutput(binding.profile, authority, lambda: 0.)
        from types import SimpleNamespace
        from engine.lightcadence_engine.effects import LightTarget
        frame = SimpleNamespace(device_id="tcp-test", target=LightTarget(0,0,1,.8))
        try:
            await adapter.acquire()
            self.reject = True
            with self.assertRaises(LanStateError):
                await adapter.send_frame(frame)
            self.reject = False
            with self.assertRaises(LanStateError):
                await adapter.send_frame(frame)
            self.assertEqual(self.connections, 1)
            self.assertEqual([m for m, _ in self.commands], ["set_scene"])
        finally:
            adapter.close()

    async def test_tcp_quota_and_identical_target_coalescing(self):
        binding = self.tcp_binding()
        authority = YeelightLanStatePort([binding], timeout=.5)
        now = [0.]
        adapter = YeelightTcpOutput(binding.profile, authority, lambda: now[0])
        from types import SimpleNamespace
        from engine.lightcadence_engine.effects import LightTarget
        a = SimpleNamespace(device_id="tcp-test", target=LightTarget(1,0,0,.5))
        b = SimpleNamespace(device_id="tcp-test", target=LightTarget(0,0,1,.5))
        try:
            await adapter.acquire()
            for index in range(60):
                now[0] = index * .9
                frame = a if index % 2 == 0 else b
                await adapter.send_frame(frame)
                await adapter.send_frame(frame)
            self.assertEqual(len(self.commands), 60)
            now[0] = 59.
            with self.assertRaises(LanStateError):
                await adapter.send_frame(a)
            self.assertEqual(len(self.commands), 60)
            now[0] = 60.
            await adapter.send_frame(a)
            self.assertEqual(len(self.commands), 61)
        finally:
            adapter.close()

    async def test_directed_identity_and_fresh_rgb(self):
        original = await self.port.read(self.profile)
        self.assertEqual(original.state, _decode("on", 60, 0xff0000))
        self.assertEqual(original.ha_registry_id, "ha-test")
        self.properties[2] = "255"
        self.assertEqual((await self.port.read(self.profile)).state,
                         _decode("on", 60, 255))
        self.assertEqual(self.connections, 2)
        self.assertEqual(len(self.probes), 2)
        self.assertTrue(self.probes[0].startswith(b"M-SEARCH * HTTP/1.1"))

    async def test_wrong_identity_or_duplicate_header_never_opens_tcp(self):
        self.lan_id = "0x9999"
        with self.assertRaises(LanStateError):
            await self.port.read(self.profile)
        self.lan_id = "0x1234"
        self.duplicate = True
        with self.assertRaises(LanStateError):
            await self.port.write(self.profile, _decode("on", 80, 255))
        self.assertEqual(self.connections, 0)

    async def test_unsupported_modes_and_uncorrelated_reply(self):
        for index, value in [(3, "2"), (4, "1"), (5, "1")]:
            old = self.properties[index]
            self.properties[index] = value
            with self.assertRaises(LanStateError):
                await self.port.read(self.profile)
            self.properties[index] = old
        self.wrong_id = True
        with self.assertRaises(LanStateError):
            await self.port.read(self.profile)
        self.assertTrue(all(m == "get_prop" for m, _ in self.commands))

    async def test_write_ack_is_not_readback_and_no_retry(self):
        target = _decode("on", 80, 255)
        self.apply = False
        self.assertTrue(await self.port.write(self.profile, target))
        self.assertNotEqual((await self.port.read(self.profile)).state, target)
        self.reject = True
        with self.assertRaises(LanStateError):
            await self.port.write(self.profile, target)
        self.assertEqual(sum(m == "set_scene" for m, _ in self.commands), 2)

    async def test_off_never_flashes_already_off_lamp(self):
        self.properties[0] = "off"
        target = _decode("off", 80, 255)
        self.assertFalse(await self.port.write(self.profile, target))
        self.assertTrue(all(m == "get_prop" for m, _ in self.commands))
        self.properties[0] = "on"
        self.assertTrue(await self.port.write(self.profile, target))
        self.assertEqual((await self.port.read(self.profile)).state, target)
        self.assertEqual([m for m, _ in self.commands if m != "get_prop"],
                         ["set_scene", "set_power"])

    async def test_explicit_composition_and_duplicate_identity(self):
        with self.assertRaises(ValueError):
            compose_lan_candidate([self.binding])
        with self.assertRaises(ValueError):
            YeelightLanStatePort([self.binding, self.binding])
        candidate = compose_lan_candidate([self.binding], enabled=True)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.probes, [])
        await candidate.close()

    async def test_original_physical_start_restore_and_identity_failure(self):
        runtime = compose_lan_candidate([self.binding], enabled=True, timeout=.5)
        command = {"op": "start", "request_id": "one", "zone_id": "zone",
                   "devices": ["test"], "effect": {"kind": "rainbow"}}
        try:
            self.lan_id = "0x9999"
            with self.assertRaises(LanStateError):
                await runtime.command(command)
            self.assertEqual(runtime.manager.snapshots(), ())
            self.assertEqual(self.chroma_messages, [])
            self.lan_id = "0x1234"
            start = await runtime.command(command)
            session = start["sessions"][0]
            self.assertTrue(any(m["method"] == "udp_sess_new" for m in self.chroma_messages))
            self.properties[2] = "255"
            stop = {"op": "stop", "session_id": session["id"],
                    "revision": session["revision"]}
            self.apply = False
            pending = await runtime.command(stop)
            self.assertEqual(pending["sessions"][0]["phase"], "stopping")
            self.assertEqual(runtime.manager.owner("test"), session["id"])
            self.apply = True
            end = await runtime.command({**stop, "revision": pending["sessions"][0]["revision"]})
            self.assertEqual(end["sessions"][0]["phase"], "stopped")
            self.assertEqual(self.properties[2], "16711680")
            self.assertIsNone(runtime.manager.owner("test"))
        finally:
            await runtime.close()


class ChromaPeer(asyncio.DatagramProtocol):
    def __init__(self, fixture):
        self.fixture = fixture

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = json.loads(data)
        self.fixture.chroma_messages.append(msg)
        if msg["method"] == "udp_sess_new":
            reply = {"id": msg["id"], "method": "udp_sess_token",
                     "params": {"token": "a" * 32}}
            self.transport.sendto(json.dumps(reply).encode(), addr)
