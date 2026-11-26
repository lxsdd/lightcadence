"""Original timeline composition, library and session tests."""
import unittest
import tempfile
from engine.lightcadence_engine.effects import decode_effect, LightTarget
from engine.lightcadence_engine.library import effect, ProjectLibrary
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.sessions import Conflict


def leaf(kind="static", color=(1, 0, 0)):
    return dict(kind=kind, period_seconds=4, brightness=1, color=list(color), width=.25)


def clip(start=0, duration=4, color=(1, 0, 0), kind="static", **kw):
    return dict(start=start, duration=duration, layer=dict(effect=leaf(kind, color), opacity=1, blend="over"),
                fade_in=0, fade_out=0, keyframes=[], **{}) | kw


def timeline(*tracks, loop=True):
    return {**leaf("timeline"), "loop": loop, "tracks": [{"name": str(i), "clips": list(t)} for i, t in enumerate(tracks)]}


class TimelineTests(unittest.TestCase):
    def test_overlap_fade_keys_and_loop(self):
        raw = timeline([clip(duration=8, color=(0, 0, 1))], [clip(start=2, duration=4, fade_in=2, fade_out=2)])
        spec = decode_effect(raw)
        self.assertEqual(spec.render(1, 0), LightTarget(0, 0, 1, 1))
        self.assertEqual(spec.render(3, 0), LightTarget(1, 0, 1, .5))
        self.assertEqual(spec.render(4, 0), LightTarget(1, 0, 0, 1))
        self.assertEqual(spec.render(9, 0), spec.render(1, 0))
        keys = [dict(time=0, value=0, curve="linear"), dict(time=2, value=1, curve="hold")]
        keyed = decode_effect(timeline([clip(keyframes=keys)], loop=False))
        self.assertEqual(keyed.render(1, 0), LightTarget(1, 0, 0, .5))
        self.assertEqual(keyed.render(4, 0), LightTarget(0, 0, 0, 0))

    def test_roundtrip_audio_invalid_and_curves(self):
        raw = timeline([clip(kind="audio_spectrum")])
        self.assertEqual(effect(raw), raw)
        with self.assertRaises(ValueError):
            decode_effect(raw)
        self.assertEqual(decode_effect({**raw, "audio_source":"pcm"}).tracks[0].clips[0].layer.effect.audio_source, "pcm")
        for bad in (timeline(), timeline([clip(duration=0)]), timeline([clip(start=True)]),
                    timeline([clip(fade_in=3, fade_out=2)]), timeline([clip(keyframes=[dict(time=5,value=1,curve="hold")])]),
                    timeline([clip(keyframes=[dict(time=1,value=1,curve="hold"),dict(time=1,value=0,curve="linear")])])):
            with self.assertRaises(ValueError):
                effect(bad)
        for curve, expected in (("hold",0),("smooth",.15625)):
            spec=decode_effect(timeline([clip(keyframes=[dict(time=0,value=0,curve=curve),dict(time=4,value=1,curve="linear")])]))
            self.assertAlmostEqual(spec.render(1,0).brightness,expected)

    def test_original_runtime_playhead_leases_and_snapshot(self):
        at = [100.]
        rt = PreviewRuntime(clock=lambda: at[0])
        raw = timeline([clip(duration=2)], loop=False)
        command = dict(op="start", request_id="r", zone_id="z", devices=["a","b"], effect=raw)
        state = rt.command(command)
        self.assertEqual(state["sessions"][0]["timeline"]["playhead"], 0)
        raw["tracks"][0]["clips"][0]["duration"] = 20
        at[0] = 103.
        rt.tick()
        state = rt.state()
        self.assertTrue(state["sessions"][0]["timeline"]["ended"])
        self.assertEqual(state["targets"]["a"]["target"]["brightness"], 0)
        with self.assertRaises(Conflict):
            rt.command(dict(op="start",request_id="other",zone_id="other",devices=["b"],effect={"kind":"static"}))
        s=state["sessions"][0]
        self.assertEqual(rt.command(dict(op="stop",session_id=s["id"],revision=s["revision"]))["targets"], {})

    def test_persistent_timeline_scene_snapshot(self):
        raw = timeline([clip(start=1, duration=3, fade_out=1)])
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectLibrary(directory)
            try:
                store.command(dict(op="library_save", revision=0, kind="zones", id=None,
                                   value={"name":"Test","devices":["a"]}))
                zone = store.snapshot()["zones"][0]["id"]
                store.command(dict(op="library_save", revision=1, kind="scenes", id=None,
                                   value={"name":"Timed","zone_id":zone,"effect":raw,"stop_policy":"restore"}))
                expected = store.snapshot()
            finally:
                store.close()
            reopened = ProjectLibrary(directory)
            try:
                self.assertEqual(reopened.snapshot(), expected)
                spec = decode_effect(reopened.snapshot()["scenes"][0]["effect"])
                self.assertEqual(spec.render(0,0).brightness, 0)
                self.assertEqual(spec.render(2,0).brightness, 1)
                self.assertEqual(spec.render(3.5,0).brightness, .5)
            finally:
                reopened.close()
