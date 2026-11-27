import asyncio
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app

TOKEN = "synthetic_test_only_" + "x" * 32


def start(request_id="a", devices=None):
    return {"op": "start", "request_id": request_id, "zone_id": request_id,
            "devices": devices or ["virtual-1"], "effect": {"kind": "rainbow"}}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = PreviewRuntime()
        self.server = TestServer(create_app(TOKEN, self.runtime))
        await self.server.start_server()
        self.session = ClientSession()
        self.headers = {"Authorization": "Bearer " + TOKEN}

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def request(self, method, path, payload=None, headers=None):
        async with self.session.request(method, self.server.make_url(path), json=payload,
                                        headers=self.headers if headers is None else headers) as response:
            return response.status, await response.json()

    async def test_auth_and_origin_rejection(self):
        for headers, expected in [({}, 401), ({"Authorization": "Bearer invalid"}, 401),
                                  ({**self.headers, "Origin": "https://untrusted.invalid"}, 403)]:
            status, _ = await self.request("POST", "/v1/preview", start(), headers)
            self.assertEqual(status, expected)
        self.assertEqual(self.runtime.manager.snapshots(), ())

    async def test_server_render_and_second_client_reconnect(self):
        code, a = await self.request("POST", "/v1/preview", start())
        self.assertEqual(code, 200)
        self.assertFalse(a["physical_output_enabled"])
        self.assertEqual(a["output_mode"], "preview")
        first = a["sessions"][0]
        seq = a["targets"]["virtual-1"]["sequence"]
        await asyncio.sleep(0.1)
        async with ClientSession() as other:
            async with other.get(self.server.make_url("/v1/state"), headers=self.headers) as response:
                b = await response.json()
        self.assertEqual(b["instance_id"], a["instance_id"])
        self.assertEqual(b["sessions"][0], first)
        self.assertGreater(b["targets"]["virtual-1"]["sequence"], seq)
        code, retry = await self.request("POST", "/v1/preview", start())
        self.assertEqual(code, 200)
        self.assertEqual(retry["sessions"], [first])

    async def test_disjoint_zones_conflict_and_stop(self):
        _, a = await self.request("POST", "/v1/preview", start("a", ["a1", "a2"]))
        first = a["sessions"][0]
        code, b = await self.request("POST", "/v1/preview", start("b", ["b1"]))
        self.assertEqual(code, 200)
        second = b["sessions"][1]
        code, conflict = await self.request("POST", "/v1/preview", start("c", ["free", "a2"]))
        self.assertEqual(code, 409)
        self.assertEqual(conflict["owners"], {"a2": first["id"]})
        self.assertIsNone(self.runtime.manager.owner("free"))
        _, detached = await self.request("POST", "/v1/preview", {"op": "detach",
            "session_id": first["id"], "revision": first["revision"], "device_id": "a1"})
        self.assertNotIn("a1", detached["targets"])
        code, err = await self.request("POST", "/v1/preview", {"op": "stop",
            "session_id": first["id"], "revision": first["revision"]})
        self.assertEqual((code, err["error"]), (409, "stale_revision"))
        code, stopped = await self.request("POST", "/v1/preview", {"op": "stop",
            "session_id": first["id"], "revision": detached["sessions"][0]["revision"]})
        self.assertEqual(code, 200)
        self.assertEqual(stopped["sessions"][0]["phase"], "stopped")
        self.assertEqual(stopped["sessions"][1]["id"], second["id"])
        self.assertEqual(set(stopped["targets"]), {"b1"})

    async def test_invalid_requests_are_atomic(self):
        for bad in ([], {"op": []}, {**start(), "physical_output_enabled": True},
                    {**start(), "devices": ["a", "a"]}, {**start(), "devices": [{}]},
                    {**start(), "effect": {"kind": "pulse", "brightness": 2}},
                    {**start(), "positions": {"virtual-1": "bad"}}):
            code, _ = await self.request("POST", "/v1/preview", bad)
            self.assertEqual(code, 400, bad)
        self.assertEqual(self.runtime.manager.snapshots(), ())

    async def test_preview_has_no_participant_limit(self):
        for n in (15, 32, 64, 128):
            code, state = await self.request("POST", "/v1/preview", start(str(n), [f"{n}-{i}" for i in range(n)]))
            self.assertEqual(code, 200)
            session = state["sessions"][-1]
            self.assertEqual(len(session["devices"]), n)
            code, _ = await self.request("POST", "/v1/preview", {"op": "stop",
                "session_id": session["id"], "revision": session["revision"]})
            self.assertEqual(code, 200)
