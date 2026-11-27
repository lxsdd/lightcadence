"""Atomic import in original engine, persistence and instance fences."""
import tempfile
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.library import ProjectLibrary
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app
from engine.lightcadence_engine.sessions import StaleRevision


def payload():
    leaf = {"kind": "static", "period_seconds": 4., "brightness": .5, "color": [1., 0., 0.], "width": .25}
    return {"op": "library_import", "revision": 0, "name": "Imported", "devices": ["a"],
            "effect": {**leaf, "kind": "layered", "brightness": 1.,
                       "layers": [{"effect": leaf, "opacity": 1., "blend": "over", "devices": ["a"]}]},
            "ha_source": {"registry_id": "scene-registry", "fingerprint": "a"*64}}


class ImportTests(unittest.IsolatedAsyncioTestCase):
    def test_atomic_persistence_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectLibrary(directory)
            with self.assertRaises(ValueError):
                store.command({**payload(), "devices": ["other"]})
            self.assertEqual(store.snapshot()["revision"], 0)
            self.assertEqual(store.snapshot()["zones"], [])
            store.command(payload())
            first = store.snapshot()
            self.assertEqual(first["revision"], 1)
            self.assertEqual(first["scenes"][0]["zone_id"], first["zones"][0]["id"])
            with self.assertRaises(StaleRevision):
                store.command(payload())
            self.assertEqual(store.snapshot(), first)
            store.close()
            reopened = ProjectLibrary(directory)
            try:
                self.assertEqual(reopened.snapshot(), first)
                scene = first["scenes"][0]
                reopened.command({"op": "library_save", "revision": 1, "kind": "scenes", "id": scene["id"],
                                  "value": {"name": "Edited", "zone_id": scene["zone_id"],
                                            "effect": scene["effect"], "stop_policy": "hold"}})
                self.assertEqual(reopened.snapshot()["scenes"][0]["ha_source"], payload()["ha_source"])
            finally:
                reopened.close()

    async def test_instance_fence_and_explicit_start(self):
        runtime = PreviewRuntime()
        token = "x"*40
        async with TestServer(create_app(token, runtime)) as server, ClientSession() as client:
            headers = {"Authorization": "Bearer " + token}
            url = server.make_url("/v1/preview")
            async with client.post(url, headers=headers, json={**payload(), "instance_id": "old"}) as res:
                self.assertEqual(res.status, 409)
            async with client.post(url, headers=headers, json={**payload(), "instance_id": runtime.instance_id}) as res:
                self.assertEqual(res.status, 200)
                state = await res.json()
            self.assertEqual(state["sessions"], [])
            self.assertEqual(state["targets"], {})
            scene = state["library"]["scenes"][0]
            started = runtime.command({"op": "scene_start", "request_id": "import-start", "scene_id": scene["id"],
                                       "library_revision": 1, "devices": ["a"]})
            target = started["targets"]["a"]["target"]
            self.assertEqual(target, {"red":1., "green":0., "blue":0., "brightness":.5})
