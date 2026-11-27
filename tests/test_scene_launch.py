"""Saved scene launch uses the original engine, authenticated HTTP and leases."""
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app
from engine.lightcadence_engine.sessions import StaleRevision

TOKEN = "synthetic_" + "x" * 32
EFFECT = {"kind": "scanner", "period_seconds": 4., "brightness": .8,
          "color": [1., 0., 0.], "width": .25}


def save(runtime, kind, value, object_id=None):
    runtime.command({"op": "library_save", "revision": runtime.library.data["revision"],
                     "kind": kind, "id": object_id, "value": value})
    return runtime.library.snapshot()[kind][-1]


def setup():
    runtime = PreviewRuntime(lambda: 0.)
    zone = save(runtime, "zones", {"name": "Test zone", "devices": ["a", "b"]})
    scene = save(runtime, "scenes", {"name": "Test scene", "zone_id": zone["id"],
                                    "effect": EFFECT, "stop_policy": "restore"})
    return runtime, zone, scene


def launch(runtime, scene, **extra):
    return {"op": "scene_start", "request_id": "launch", "scene_id": scene["id"],
            "library_revision": runtime.library.data["revision"], "devices": ["a", "b"], **extra}


class SceneLaunchTests(unittest.TestCase):
    def test_snapshot_and_replay_after_library_edit_and_stop(self):
        runtime, zone, scene = setup()
        body = launch(runtime, scene)
        result = runtime.command(body)
        original = result["sessions"][0]
        self.assertEqual(original["zone_id"], zone["id"])
        self.assertEqual(original["scene_origin"]["scene_id"], scene["id"])
        self.assertEqual(original["scene_origin"]["scene_revision"], 1)
        self.assertEqual(original["effect"]["brightness"], .8)
        save(runtime, "scenes", {"name": "Changed", "zone_id": zone["id"],
                                 "effect": {**EFFECT, "brightness": .2}, "stop_policy": "off"}, scene["id"])
        self.assertEqual(runtime.command(body)["sessions"], [original])
        runtime.command({"op": "detach", "session_id": original["id"],
                         "revision": original["revision"], "device_id": "a"})
        current = runtime.manager.snapshot(original["id"])
        runtime.command({"op": "stop", "session_id": current.id, "revision": current.revision})
        self.assertEqual(runtime.command(body)["sessions"][0]["phase"], "stopped")
        self.assertIsNone(runtime.manager.owner("a"))
        self.assertIsNone(runtime.manager.owner("b"))
        with self.assertRaises(ValueError):
            runtime.command({**body, "scene_id": "different"})

    def test_stale_selection_and_audio_fail_before_admission(self):
        runtime, zone, scene = setup()
        body = launch(runtime, scene)
        with self.assertRaises(StaleRevision):
            runtime.command({**body, "library_revision": 0})
        with self.assertRaises(StaleRevision):
            runtime.command({**body, "devices": ["b", "a"]})
        for extra in ({"path_id": "missing"}, {"floorplan_id": "x", "path_id": "y"},
                      {"audio_source": "unexpected"}, {"effect": EFFECT}):
            with self.assertRaises(ValueError):
                runtime.command({**body, **extra})
        audio = save(runtime, "scenes", {"name": "Audio", "zone_id": zone["id"],
            "effect": {**EFFECT, "kind": "audio_spectrum"}, "stop_policy": "restore"})
        for extra in ({}, {"audio_source": "missing"}):
            with self.assertRaises(ValueError):
                runtime.command(launch(runtime, audio, **extra))
        self.assertEqual(runtime.manager.snapshots(), ())
        source = runtime.audio.open("Synthetic")
        self.assertEqual(runtime.command(launch(runtime, audio, audio_source=source))["sessions"][0]["effect"]["audio_source"], source)

    def test_spatial_path_and_other_zone(self):
        runtime, zone, scene = setup()
        def feature(id, kind, points, device=None, zone_id=None):
            return {"id": id, "kind": kind, "name": id, "points": points,
                    "device_id": device, "zone_id": zone_id}
        floor = save(runtime, "floorplans", {"name": "Floor", "width": 10, "height": 5,
            "features": [feature("a", "lamp", [[0,0]], "a"),
                         feature("b", "lamp", [[1,0]], "b"),
                         feature("path", "effect_path", [[0,0],[10,0]], zone_id=zone["id"])]})
        runtime.command({"op": "start", "request_id": "other", "zone_id": "other",
                         "devices": ["c"], "effect": {"kind": "static"}})
        result = runtime.command(launch(runtime, scene, floorplan_id=floor["id"], path_id="path"))
        self.assertEqual(result["sessions"][1]["scene_origin"]["path_id"], "path")
        self.assertAlmostEqual(result["targets"]["b"]["target"]["brightness"], .48)
        self.assertEqual(result["sessions"][0]["phase"], "running")


class SceneLaunchHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_instance_and_revision(self):
        runtime, _, scene = setup()
        body = {**launch(runtime, scene), "instance_id": runtime.instance_id}
        headers = {"Authorization": "Bearer " + TOKEN}
        async with TestServer(create_app(TOKEN, runtime)) as server, ClientSession() as client:
            url = server.make_url("/v1/preview")
            async def post(payload, h):
                async with client.post(url, headers=h, json=payload) as response:
                    return response.status, await response.json()
            self.assertEqual((await post(body, {}))[0], 401)
            self.assertEqual((await post({**body, "instance_id": "old"}, headers))[0], 409)
            self.assertEqual((await post({**body, "library_revision": 0}, headers))[0], 409)
            status, result = await post(body, headers)
            self.assertEqual(status, 200)
            self.assertEqual(result["sessions"][0]["scene_origin"]["scene_id"], scene["id"])
            status, replay = await post(body, headers)
            self.assertEqual(status, 200)
            self.assertEqual(replay["sessions"], result["sessions"])
