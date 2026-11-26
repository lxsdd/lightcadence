"""Original scene/track selection, end policies and session ownership."""
import unittest
from engine.lightcadence_engine.effects import decode_effect
from engine.lightcadence_engine.library import ProjectLibrary, effect
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.sessions import Phase, Conflict
from test_layers import stack, layer
from test_timeline import timeline, clip


class TargetTests(unittest.TestCase):
    def test_layers_and_tracks_render_only_assigned_lamps(self):
        for raw in (stack({**layer(color=(0,0,1)), "devices":["a"]},
                          {**layer(), "devices":["b"]}),
                    {**timeline([clip(color=(0,0,1))],[clip()]), "tracks":[
                        {"name":"blue","devices":["a"],"clips":[clip(color=(0,0,1))]},
                        {"name":"red","devices":["b"],"clips":[clip()]}]}):
            self.assertEqual(effect(raw),raw)
            rt=PreviewRuntime(clock=lambda:10.)
            state=rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a","b"],effect=raw))
            self.assertEqual(state["targets"]["a"]["target"]["blue"],1.)
            self.assertEqual(state["targets"]["a"]["target"]["red"],0.)
            self.assertEqual(state["targets"]["b"]["target"]["red"],1.)
            self.assertEqual(state["targets"]["b"]["target"]["blue"],0.)
            s=rt.manager.snapshots()[0]
            rt.command(dict(op="detach",session_id=s.id,revision=s.revision,device_id="a"))
            self.assertIsNone(rt.manager.owner("a"))
            self.assertEqual(set(rt.state()["targets"]),{"b"})

    def test_invalid_mask_or_uncovered_target_rejected_before_lease(self):
        for mask in ([], ["a","a"], ["foreign"], ["a"], None):
            raw=stack({**layer(),"devices":mask})
            rt=PreviewRuntime()
            with self.assertRaises(ValueError):
                rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a","b"],effect=raw))
            self.assertEqual(rt.manager.snapshots(),())
        store=ProjectLibrary()
        store.command(dict(op="library_save",revision=0,kind="zones",id=None,
                           value={"name":"Test","devices":["a","b"]}))
        zid=store.snapshot()["zones"][0]["id"]
        raw=stack({**layer(),"devices":["a"]},{**layer(),"devices":["b"]})
        store.command(dict(op="library_save",revision=1,kind="scenes",id=None,
                           value={"name":"Scene","zone_id":zid,"effect":raw,"stop_policy":"restore"}))
        with self.assertRaises(ValueError):
            store.command(dict(op="library_save",revision=2,kind="zones",id=zid,
                               value={"name":"Test","devices":["a"]}))
        self.assertEqual(store.snapshot()["revision"],2)

    def test_one_shot_auto_end_respects_pause_and_detached_lamps(self):
        at=[10.]
        rt=PreviewRuntime(clock=lambda:at[0])
        raw={**timeline([clip(duration=2)],loop=False),"auto_end":True}
        self.assertEqual(effect(raw),raw)
        with self.assertRaises(ValueError):
            decode_effect({**raw,"loop":True})
        rt.command(dict(op="start",request_id="r",zone_id="z",devices=["a","b"],effect=raw))
        s=rt.manager.snapshots()[0]
        rt.command(dict(op="detach",session_id=s.id,revision=s.revision,device_id="a"))
        rt.command(dict(op="start",request_id="other",zone_id="other",devices=["a"],effect={"kind":"static"}))
        s=rt.manager.snapshot(s.id)
        rt.command(dict(op="timeline_control",session_id=s.id,revision=s.revision,action="pause"))
        at[0]=20.
        rt.tick()
        self.assertEqual(rt.manager.owner("b"),s.id)
        s=rt.manager.snapshot(s.id)
        rt.command(dict(op="timeline_control",session_id=s.id,revision=s.revision,action="seek",position=2.))
        self.assertEqual(rt.manager.owner("b"),s.id)
        s=rt.manager.snapshot(s.id)
        rt.command(dict(op="timeline_control",session_id=s.id,revision=s.revision,action="resume"))
        self.assertEqual(rt.manager.snapshot(s.id).phase,Phase.STOPPED)
        self.assertIsNone(rt.manager.owner("b"))
        self.assertIsNotNone(rt.manager.owner("a"))
        self.assertEqual(set(rt.state()["targets"]),{"a"})
