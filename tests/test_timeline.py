"""Original timeline composition, library and session tests."""
import unittest
import tempfile
from engine.lightcadence_engine.effects import decode_effect, LightTarget
from engine.lightcadence_engine.library import effect, ProjectLibrary
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.sessions import Conflict, SessionError, StaleRevision
from engine.lightcadence_engine.server import create_app
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer


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

    def test_transport_revision_clock_generation_and_other_zone(self):
        at = [100.]
        rt = PreviewRuntime(clock=lambda: at[0])
        raw = timeline([clip(duration=2),clip(start=2,duration=2,color=(0,0,1))], loop=False)
        rt.command(dict(op="start",request_id="timed",zone_id="timed",devices=["a"],effect=raw))
        rt.command(dict(op="start",request_id="other",zone_id="other",devices=["b"],effect={"kind":"pulse"}))
        s, other = rt.manager.snapshots()
        at[0]=101.
        rt.manager.render(s.id)
        queued=rt.manager.peek_latest("a")
        paused=rt.manager.timeline_control(s.id,s.revision,"pause")
        self.assertFalse(rt.manager.frame_is_current(queued))
        self.assertIsNone(rt.manager.peek_latest("a"))
        with self.assertRaises(StaleRevision):
            rt.manager.timeline_control(s.id,s.revision,"seek",3.)
        for position in (True,-1,5,float("nan")):
            with self.assertRaises(SessionError):
                rt.manager.timeline_control(s.id,paused.revision,"seek",position)
        at[0]=110.
        rt.tick()
        self.assertEqual(rt.manager.timeline_state(s.id)["playhead"],1.)
        self.assertEqual(rt.state()["targets"]["a"]["target"]["red"],1.)
        self.assertEqual(rt.manager.snapshot(other.id).revision,other.revision)
        self.assertEqual(rt.manager.owner("a"),s.id)
        state=rt.command(dict(op="timeline_control",session_id=s.id,revision=paused.revision,action="seek",position=3.))
        self.assertEqual(state["targets"]["a"]["target"]["blue"],1.)
        self.assertTrue(state["sessions"][0]["timeline"]["paused"])
        now=rt.manager.snapshot(s.id)
        rt.command(dict(op="timeline_control",session_id=s.id,revision=now.revision,action="resume"))
        at[0]=111.
        rt.tick()
        self.assertTrue(rt.state()["sessions"][0]["timeline"]["ended"])
        self.assertEqual(rt.state()["targets"]["a"]["target"]["brightness"],0.)
        self.assertEqual(rt.manager.owner("a"),s.id)
        now=rt.manager.snapshot(s.id)
        rt.command(dict(op="stop",session_id=s.id,revision=now.revision))
        self.assertIsNone(rt.manager.owner("a"))
        self.assertEqual(rt.manager.owner("b"),other.id)

    def test_paused_audio_is_frozen_and_loop_seek_wraps(self):
        at=[10.]
        rt=PreviewRuntime(clock=lambda:at[0])
        bands=[(1.,0.,0.)]
        source=rt.audio.open("synthetic")
        rt.manager.audio_provider=lambda source,now:bands[0]
        raw={**timeline([clip(kind="audio_spectrum")]),"audio_source":source}
        rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a"],effect=raw))
        s=rt.manager.snapshots()[0]
        p=rt.manager.timeline_control(s.id,s.revision,"pause")
        bands[0]=(0.,0.,0.)
        at[0]=11.
        rt.tick()
        self.assertEqual(rt.state()["targets"]["a"]["target"]["brightness"],1.)
        q=rt.manager.timeline_control(s.id,p.revision,"seek",4.)
        self.assertEqual(rt.manager.timeline_state(s.id)["playhead"],0.)
        rt.manager.timeline_control(s.id,q.revision,"resume")
        rt.tick()
        self.assertEqual(rt.state()["targets"]["a"]["target"]["brightness"],0.)


class TimelineHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_instance_and_revision_fence(self):
        rt=PreviewRuntime()
        raw=timeline([clip(duration=8)])
        rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a"],effect=raw))
        s=rt.manager.snapshots()[0]
        token="synthetic_"+"x"*32
        headers={"Authorization":"Bearer "+token}
        body=dict(op="timeline_control",session_id=s.id,revision=s.revision,action="pause",instance_id=rt.instance_id)
        async with TestServer(create_app(token,rt)) as server, ClientSession() as client:
            url=server.make_url("/v1/preview")
            async with client.post(url,json=body) as res:
                self.assertEqual(res.status,401)
            async with client.post(url,json={**body,"instance_id":"old"},headers=headers) as res:
                self.assertEqual(res.status,409)
            async with client.post(url,json=body,headers=headers) as res:
                self.assertEqual(res.status,200)
                state=await res.json()
            self.assertTrue(state["sessions"][0]["timeline"]["paused"])
            async with client.post(url,json=body,headers=headers) as res:
                self.assertEqual(res.status,409)
            current=rt.manager.snapshot(s.id)
            async with client.post(url,json={**body,"revision":current.revision,"position":1},headers=headers) as res:
                self.assertEqual(res.status,400)
