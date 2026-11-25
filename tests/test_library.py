"""Original library operations, persistence and authenticated runtime routing."""
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.library import ProjectLibrary
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app
from engine.lightcadence_engine.sessions import StaleRevision


def cmd(revision, kind, value, id=None, op="library_save"):
    return dict(op=op, revision=revision, kind=kind, id=id, value=value)


ZONE = {"name": "Synthetic zone", "devices": ["b", "a"]}
EFFECT = {"kind": "scanner", "period_seconds": 2., "brightness": .7, "color": [1., 0., 0.], "width": .25}


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    def test_roundtrip_order_revision_and_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectLibrary(directory)
            store.command(cmd(0, "zones", ZONE))
            zone = store.snapshot()["zones"][0]
            scene = {"name": "Test scene", "zone_id": zone["id"], "effect": EFFECT, "stop_policy": "restore"}
            store.command(cmd(1, "scenes", scene))
            with self.assertRaises(StaleRevision):
                store.command(cmd(1, "zones", ZONE))
            with self.assertRaises(ValueError):
                store.command(cmd(2, "zones", None, zone["id"], "library_delete"))
            with self.assertRaises(OSError):
                ProjectLibrary(directory)
            snapshot = store.snapshot()
            store.close()
            other = ProjectLibrary(directory)
            try:
                self.assertEqual(other.snapshot(), snapshot)
                self.assertEqual(other.snapshot()["zones"][0]["devices"], ["b", "a"])
                other.command(cmd(2, "zones", {**ZONE, "name": "Renamed"}, zone["id"]))
                self.assertEqual(other.snapshot()["zones"][0]["revision"], 2)
                self.assertEqual(other.snapshot()["zones"][0]["id"], zone["id"])
            finally:
                other.close()

    def test_invalid_input_no_commit(self):
        store = ProjectLibrary()
        for bad in ({**ZONE, "devices": ["a", "a"]}, {**ZONE, "devices": []}, {**ZONE, "extra": True}):
            with self.assertRaises(ValueError):
                store.command(cmd(0, "zones", bad))
        self.assertEqual(store.snapshot()["revision"], 0)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "library-v1.json").write_text('{"schema_version":2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                ProjectLibrary(directory)

    async def test_http_instance_auth_and_original_preview(self):
        runtime = PreviewRuntime()
        token = "x" * 40
        async with TestServer(create_app(token, runtime)) as server, ClientSession() as client:
            url = server.make_url("/v1/preview")
            body = {**cmd(0, "zones", ZONE), "instance_id": runtime.instance_id}
            async with client.post(url, json=body) as res:
                self.assertEqual(res.status, 401)
            headers = {"Authorization": "Bearer " + token}
            async with client.post(url, headers=headers, json={**body, "instance_id": "old"}) as res:
                self.assertEqual(res.status, 409)
            async with client.post(url, headers=headers, json=body) as res:
                self.assertEqual(res.status, 200)
                snapshot = await res.json()
            self.assertEqual(snapshot["sessions"], [])
            self.assertEqual(snapshot["library"]["zones"][0]["devices"], ["b", "a"])
            async with client.post(url, headers=headers, json=body) as res:
                self.assertEqual(res.status, 409)
            self.assertEqual(runtime.manager.snapshots(), ())
