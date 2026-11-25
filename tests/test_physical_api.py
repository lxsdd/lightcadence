"""Authenticated original HTTP/renderer/UDP transaction, synthetic state authority."""
import asyncio
import json
import socket
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.effects import LightTarget
from engine.lightcadence_engine.enrollment import ChromaProfile, Observation
from engine.lightcadence_engine.runtime import PhysicalRuntime
from engine.lightcadence_engine.server import create_app
from engine.lightcadence_engine.sessions import DeviceState

TOKEN = "synthetic_only_" + "x" * 32
ORIGINAL = DeviceState(True, LightTarget(.5, .2, .1, .6))
MANUAL = {"power": True, "target": {"red": 0, "green": 1, "blue": 0, "brightness": .8}}


class StateAuthority:
    def __init__(self):
        self.value = ORIGINAL
        self.writes = []
        self.apply = True
        self.wrong = False

    async def read(self, p):
        return Observation("wrong" if self.wrong else p.device_id,
                           p.ha_registry_id, p.model, p.firmware, "rgb", self.value)

    async def write(self, p, state):
        self.writes.append(state)
        if self.apply:
            self.value = state
        return True


class PhysicalApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.peer.bind(("127.0.0.1", 0))
        self.peer.setblocking(False)
        self.addCleanup(self.peer.close)
        self.profile = ChromaProfile("lamp", "ha-id", "synthetic", "test",
                                     "127.0.0.1", .02, self.peer.getsockname()[1])
        self.authority = StateAuthority()
        self.runtime = PhysicalRuntime([self.profile], self.authority, enabled=True)
        self.server = TestServer(create_app(TOKEN, self.runtime))
        await self.server.start_server()
        self.client = ClientSession()
        self.headers = {"Authorization": "Bearer " + TOKEN}

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def request(self, body=None, path="/v1/physical", headers=None):
        if body is not None and path == "/v1/physical":
            body = {"instance_id": self.runtime.instance_id, **body}
        async with self.client.request("GET" if body is None else "POST",
                                       self.server.make_url(path), json=body,
                                       headers=self.headers if headers is None else headers) as response:
            if response.content_type != "application/json":
                return response.status, None
            return response.status, await response.json()

    async def receive(self):
        async with asyncio.timeout(1):
            raw, addr = await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
        return json.loads(raw), addr

    def start_body(self, rid="start"):
        return {"op": "start", "request_id": rid, "zone_id": "zone", "devices": ["lamp"],
                "effect": {"kind": "rainbow"}}

    async def start(self):
        request = asyncio.create_task(self.request(self.start_body()))
        msg, addr = await self.receive()
        self.assertEqual(msg["method"], "udp_sess_new")
        self.peer.sendto(json.dumps({"id": msg["id"], "method": "udp_sess_token",
                                    "params": {"token": "a" * 32}}).encode(), addr)
        code, state = await request
        self.assertEqual(code, 200)
        return state

    async def test_default_no_physical_route_and_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            PhysicalRuntime([self.profile], self.authority)
        async with TestServer(create_app(TOKEN)) as preview:
            async with self.client.post(preview.make_url("/v1/physical"),
                                        json=self.start_body(), headers=self.headers) as response:
                self.assertEqual(response.status, 404)
        code, _ = await self.request(self.start_body(), path="/v1/preview")
        self.assertEqual(code, 404)
        self.assertEqual(self.runtime.manager.snapshots(), ())

    async def test_auth_validation_and_identity_fail_before_udp(self):
        for headers, code in [({}, 401), ({**self.headers, "Origin": "https://invalid.test"}, 403)]:
            self.assertEqual((await self.request(self.start_body(), headers=headers))[0], code)
        self.assertEqual((await self.request({**self.start_body(), "devices": ["lamp", "lamp"]}))[0], 400)
        self.assertEqual((await self.request({**self.start_body(), "stop_policy": "invalid"}))[0], 400)
        self.authority.wrong = True
        self.assertEqual((await self.request(self.start_body()))[0], 400)
        self.assertEqual(self.runtime.manager.snapshots(), ())
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)

    async def test_stale_instance_cannot_write_or_start(self):
        for payload in (self.start_body(), {"op": "manual_set", "request_id": "manual", "device_id": "lamp", "state": MANUAL}):
            code, error = await self.request({**payload, "instance_id": "old-engine"})
            self.assertEqual((code, error["error"]), (409, "stale_instance"))
        self.assertEqual(self.runtime.manager.snapshots(), ())
        self.assertEqual(self.authority.writes, [])

    async def test_render_reconnect_conflict_and_confirmed_restore(self):
        state = await self.start()
        self.assertEqual(state["output_mode"], "physical_candidate")
        self.assertFalse(state["hardware_qualified"])
        self.assertEqual(state["enrolled_devices"], ["lamp"])
        self.assertIn("lamp", state["targets"])
        session = state["sessions"][0]
        msg, _ = await self.receive()
        self.assertEqual(msg["method"], "set_scene")
        _, latest = await self.request(path="/v1/state")
        self.assertEqual(latest["transport_status"]["lamp"]["outcome"], "submitted_unverified")
        self.assertEqual(latest["sessions"][0]["id"], session["id"])
        _, replay = await self.request(self.start_body())
        self.assertEqual(len(replay["sessions"]), 1)
        self.assertEqual((await self.request(self.start_body("other")))[0], 409)
        stop = {"op": "stop", "session_id": session["id"], "revision": session["revision"]}
        self.authority.value = DeviceState(False, ORIGINAL.target)
        _, end = await self.request(stop)
        self.assertEqual(end["sessions"][0]["phase"], "stopped")
        self.assertEqual(end["targets"], {})
        self.assertEqual(self.authority.writes, [ORIGINAL])
        self.assertEqual(self.authority.value, ORIGINAL)
        self.assertIsNone(self.runtime.manager.owner("lamp"))

    async def test_unconfirmed_restore_holds_lease_until_retry(self):
        state = await self.start()
        s = state["sessions"][0]
        self.authority.apply = False
        self.authority.value = DeviceState(False, ORIGINAL.target)
        stop = {"op": "stop", "session_id": s["id"], "revision": s["revision"]}
        _, pending = await self.request(stop)
        self.assertEqual(pending["sessions"][0]["phase"], "stopping")
        self.assertEqual(self.runtime.manager.owner("lamp"), s["id"])
        self.assertEqual((await self.request(self.start_body("other")))[0], 409)
        self.assertEqual((await self.request(stop))[0], 409)
        self.authority.apply = True
        _, end = await self.request({**stop, "revision": pending["sessions"][0]["revision"]})
        self.assertEqual(end["sessions"][0]["phase"], "stopped")

    async def test_manual_handover_idempotent_never_restores(self):
        state = await self.start()
        s = state["sessions"][0]
        command = {"op": "manual_set", "request_id": "manual", "device_id": "lamp", "state": MANUAL}
        _, end = await self.request(command)
        self.assertEqual(end["manual_result"], "confirmed")
        self.assertEqual(end["sessions"][0]["phase"], "stopped")
        self.assertEqual(end["targets"], {})
        _, replay = await self.request(command)
        self.assertEqual(replay["manual_result"], "confirmed")
        self.assertEqual(len(self.authority.writes), 1)
        self.assertEqual((await self.request({**command, "state": {**MANUAL, "power": False}}))[0], 400)
        await self.request({"op": "stop", "session_id": s["id"], "revision": end["sessions"][0]["revision"]})
        self.assertEqual(len(self.authority.writes), 1)
        self.assertFalse(self.runtime.physical._adapters["lamp"].transport.ready)

    async def test_manual_unconfirmed_is_not_replayed(self):
        await self.start()
        self.authority.apply = False
        command = {"op": "manual_set", "request_id": "manual", "device_id": "lamp", "state": MANUAL}
        _, first = await self.request(command)
        self.assertEqual(first["manual_result"], "unconfirmed")
        self.authority.apply = True
        _, second = await self.request(command)
        self.assertEqual(second["manual_result"], "unconfirmed")
        self.assertEqual(len(self.authority.writes), 1)

    async def test_shutdown_closes_local_transport_without_restore_claim(self):
        await self.start()
        await self.server.close()
        self.assertFalse(self.runtime.physical._adapters["lamp"].transport.ready)
        self.assertEqual(self.authority.writes, [])
