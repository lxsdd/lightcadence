"""Actual original LAN state port and candidate composition, loopback peers only."""
import asyncio
import json
import unittest

from engine.lightcadence_engine.enrollment import ChromaProfile
from engine.lightcadence_engine.yeelight_lan import (
    LanBinding, LanStateError, YeelightLanStatePort, compose_lan_candidate,
    _decode,
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
                   "support: get_prop set_scene set_power"]
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
                    response = {"id": rid, "result": self.properties.copy()}
                elif self.reject:
                    response = {"id": rid, "error": {"code": -1}}
                else:
                    if self.apply:
                        if method == "set_scene":
                            _, rgb, bright = obj["params"]
                            self.properties[:4] = ["on", str(bright), str(rgb), "1"]
                        elif method == "set_power":
                            self.properties[0] = obj["params"][0]
                    response = {"id": rid, "result": ["ok"]}
                writer.write(json.dumps(response).encode() + b"\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

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
