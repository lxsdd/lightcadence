"""Original persisted geometry, effect rendering and rejection regressions."""
import json
from pathlib import Path
import tempfile
import unittest

from engine.lightcadence_engine.library import ProjectLibrary
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.sessions import StaleRevision


def command(rev, kind, value, id=None, op="library_save"):
    return dict(op=op, revision=rev, kind=kind, value=value, id=id)


def feature(id, kind, points, device=None, zone=None):
    return dict(id=id, kind=kind, name=id, points=points, device_id=device, zone_id=zone)


def plan(zone):
    return dict(name="Synthetic floor", width=10, height=10, features=[
        feature("room", "room", [[0,0],[10,0],[10,10],[0,10]]),
        feature("a", "lamp", [[0,0]], "a"),
        feature("b", "lamp", [[10,5]], "b"),
        feature("strip", "strip", [[0,0],[5,0],[10,0]], "c"),
        feature("path", "effect_path", [[0,0],[10,0],[10,10]], zone=zone),
    ])


class SpatialTests(unittest.TestCase):
    def test_projection_into_original_renderer(self):
        runtime = PreviewRuntime(clock=lambda: 10.)
        lib = runtime.library
        lib.command(command(0, "zones", dict(name="Zone", devices=["b","a","c"])))
        zone = lib.snapshot()["zones"][0]["id"]
        lib.command(command(1, "floorplans", plan(zone)))
        positions = lib.snapshot()["spatial_paths"][0]["positions"]
        self.assertEqual(positions, {"a":0., "b":.75, "c":.25})
        state = runtime.command(dict(op="start", request_id="test", zone_id="Zone",
            devices=["b","a","c"], effect={"kind":"scanner","width":.5}, positions=positions))
        self.assertEqual(state["targets"]["a"]["target"]["brightness"], 1.)
        self.assertEqual(state["targets"]["b"]["target"]["brightness"], 0.)
        self.assertEqual(state["targets"]["c"]["target"]["brightness"], .5)
        floor = lib.snapshot()["floorplans"][0]["id"]
        lib.command(command(2,"floorplans",None,floor,"library_delete"))
        self.assertEqual(len(runtime.manager.snapshots()),1)

    def test_incomplete_and_invalid_geometry(self):
        lib = ProjectLibrary()
        lib.command(command(0,"zones",dict(name="Zone",devices=["a","missing"])))
        zone = lib.snapshot()["zones"][0]["id"]
        data = plan(zone)
        lib.command(command(1,"floorplans",data))
        result = lib.snapshot()["spatial_paths"][0]
        self.assertIsNone(result["positions"])
        self.assertEqual(result["missing"],["missing"])
        for bad in ([[-1,0]], [[True,0]], [[float("nan"),0]], [[1,1],[2,2]]):
            value = plan(zone)
            value["features"][1]["points"] = bad
            with self.assertRaises(ValueError):
                lib.command(command(2,"floorplans",value))
        with self.assertRaises(ValueError):
            lib.command(command(2,"zones",None,zone,"library_delete"))
        with self.assertRaises(StaleRevision):
            lib.command(command(1,"floorplans",data))
        self.assertEqual(lib.snapshot()["revision"],2)

    def test_old_document_migration_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,"library-v1.json").write_text(json.dumps(
                dict(schema_version=1,revision=0,zones=[],scenes=[])),encoding="utf-8")
            lib = ProjectLibrary(directory)
            self.assertEqual(lib.snapshot()["floorplans"],[])
            lib.command(command(0,"zones",dict(name="Zone",devices=["a","b","c"])))
            zone = lib.snapshot()["zones"][0]["id"]
            lib.command(command(1,"floorplans",plan(zone)))
            snapshot = lib.snapshot()
            lib.close()
            reopened = ProjectLibrary(directory)
            try:
                self.assertEqual(reopened.snapshot(),snapshot)
            finally:
                reopened.close()
