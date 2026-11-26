"""Musical time is authored in beats, engine transport remains monotonic seconds."""
import unittest
from engine.lightcadence_engine.effects import decode_effect, encode_preset, LightTarget
from engine.lightcadence_engine.runtime import PreviewRuntime
def clip(start=0,duration=4,color=(1,0,0),**kw):
    return dict(start=start,duration=duration,fade_in=0,fade_out=0,keyframes=[],
                layer=dict(effect=dict(kind="static",period_seconds=4,brightness=1,color=list(color),width=.25),opacity=1,blend="over")) | kw


def timeline(*tracks,loop=True):
    return dict(kind="timeline",period_seconds=4,brightness=1,color=[1,0,0],width=.25,loop=loop,
                tracks=[dict(name=str(i),clips=list(t)) for i,t in enumerate(tracks)])


class MusicalTimelineTests(unittest.TestCase):
    def test_tempo_boundaries_and_keyframe_render(self):
        raw = timeline([clip(duration=8, keyframes=[dict(time=0,value=0,curve="linear"),dict(time=8,value=1,curve="linear")])], loop=False)
        raw.update(tempo_map=[dict(beat=0,bpm=120),dict(beat=4,bpm=60)],
                   markers=[dict(time=4,name="Bridge")])
        spec = decode_effect(raw)
        self.assertEqual(encode_preset(spec), raw)
        self.assertEqual(spec.duration, 6.)
        self.assertEqual(spec.seconds(4), 2.)
        self.assertEqual(spec.authored_time(4), 6.)
        self.assertAlmostEqual(spec.render(4,0).brightness,.75)
        self.assertEqual(spec.render(6,0),LightTarget(0,0,0,0))
        self.assertEqual(decode_effect(timeline([clip(duration=8)])).duration,8.)

    def test_invalid_maps_and_markers(self):
        raw=timeline([clip(duration=8)])
        for tempo in ([],[dict(beat=1,bpm=120)],[dict(beat=0,bpm=True)],
                      [dict(beat=0,bpm=0)],[dict(beat=0,bpm=120),dict(beat=0,bpm=60)],
                      [dict(beat=0,bpm=120),dict(beat=9,bpm=60)]):
            with self.assertRaises(ValueError):
                decode_effect({**raw,"tempo_map":tempo})
        for markers in ([dict(time=9,name="Outside")],[dict(time=1,name="")],
                        [dict(time=2,name="B"),dict(time=1,name="A")]):
            with self.assertRaises(ValueError):
                decode_effect({**raw,"markers":markers})
        with self.assertRaises(ValueError):
            decode_effect(dict(kind="static",tempo_map=[dict(beat=0,bpm=120)]))

    def test_runtime_marker_seek_and_loop(self):
        clock=[10.]
        rt=PreviewRuntime(clock=lambda:clock[0])
        raw=timeline([clip(duration=4),clip(start=4,duration=4,color=(0,0,1))])
        raw.update(tempo_map=[dict(beat=0,bpm=120),dict(beat=4,bpm=60)],markers=[dict(time=4,name="Bridge")])
        state=rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a"],effect=raw))
        s=state["sessions"][0]
        self.assertEqual(s["timeline"]["duration"],6.)
        self.assertEqual(s["timeline"]["markers"][0]["seconds"],2.)
        state=rt.command(dict(op="timeline_control",session_id=s["id"],revision=s["revision"],action="seek",position=2.))
        self.assertEqual(state["sessions"][0]["timeline"]["beat"],4.)
        self.assertEqual(state["targets"]["a"]["target"]["blue"],1.)
        clock[0]+=6.
        rt.tick()
        self.assertEqual(rt.state()["sessions"][0]["timeline"]["beat"],4.)
