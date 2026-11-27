"""Original common renderer, library and physical compare tests, no hardware."""
import tempfile
import unittest
from engine.lightcadence_engine.calibration import Correction
from engine.lightcadence_engine.effects import LightTarget, linear_channel, Effect, EffectKind
from engine.lightcadence_engine.library import ProjectLibrary
from engine.lightcadence_engine.runtime import PhysicalRuntime
from engine.lightcadence_engine.enrollment import ChromaProfile, Observation
from engine.lightcadence_engine.sessions import DeviceState, Conflict, StaleRevision

BIND={"model":"synthetic","firmware":"test","transport":"chroma_v2"}
VALUE={"name":"Warm correction","selector":BIND,"points":[{"goal":[255,100,0],"command":[220,120,10]}],
       "brightness":[{"goal":.5,"command":.4}],"minimum":.08}
ORIGINAL=DeviceState(True,LightTarget(.4,.2,.1,.3))
class Authority:
    def __init__(self): self.states={}
    async def read(self,p):
        return Observation(p.device_id,p.ha_registry_id,p.model,p.firmware,"rgb",self.states.get(p.device_id,ORIGINAL))
    async def write(self,p,s): self.states[p.device_id]=s; return True
class Adapter:
    def __init__(self,p): self.frames=[];self.closed=False
    async def acquire(self): self.closed=False
    async def maintain(self): pass
    async def send_frame(self,frame): self.frames.append(frame)
    def close(self): self.closed=True
class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_exact_anchor_and_dark(self):
        c=Correction.decode(VALUE)
        t=LightTarget(*(linear_channel(v) for v in [255,100,0]),.5)
        self.assertEqual(c.apply(t),LightTarget(*(linear_channel(v) for v in [220,120,10]),.4))
        self.assertEqual(c.apply(LightTarget(0,0,0,.5)),LightTarget(0,0,0,.5))
        self.assertEqual(c.apply(LightTarget(1,1,1,0)),LightTarget(1,1,1,0))
        self.assertEqual(c.apply(LightTarget(1,1,1,.001)).brightness,.08)
        for bad in ({**VALUE,"points":VALUE["points"]*2},{**VALUE,"brightness":[{"goal":.5,"command":.6},{"goal":.8,"command":.4}]},{**VALUE,"minimum":float("nan")}):
            with self.assertRaises(ValueError): Correction.decode(bad)
    def test_library_revision_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            lib=ProjectLibrary(d)
            cmd=dict(op="library_save",kind="calibrations",id=None,revision=0,value=VALUE)
            lib.command(cmd)
            with self.assertRaises(ValueError): lib.command({**cmd,"revision":1})
            with self.assertRaises(StaleRevision): lib.command(cmd)
            saved=lib.snapshot();lib.close()
            lib=ProjectLibrary(d)
            self.assertEqual(lib.snapshot(),saved);lib.close()
    async def test_original_physical_comparison_and_common_correction(self):
        auth=Authority()
        profiles=[ChromaProfile(d,d,"synthetic","test","127.0.0.1",0,port) for d,port in (("a",10001),("b",10002))]
        runtime=PhysicalRuntime(profiles,auth,enabled=True,transport_factory=lambda p,c:Adapter(p))
        try:
            cmd=dict(op="library_save",kind="calibrations",id=None,revision=0,value=VALUE)
            await runtime.command(cmd)
            proposal=dict(op="calibration_project",revision=1,devices=["a","b"],
                targets={d:{"rgb":[255,100,0],"brightness":.5} for d in ("a","b")})
            result=await runtime.command(proposal)
            self.assertEqual(result["calibration_projection"]["targets"]["a"]["ha"],
                {"state":"on","rgb_color":[220,120,10],"brightness":102})
            self.assertEqual(auth.states,{})
            self.assertEqual(runtime.manager.snapshots(),())
            with self.assertRaises(StaleRevision): await runtime.command({**proposal,"revision":0})
            start=dict(op="calibration_start",request_id="compare",devices=["a","b"],reference="a",
                targets={"a":{"rgb":[255,100,0],"brightness":.5},"b":{"rgb":[220,120,10],"brightness":.4}})
            state=await runtime.command(start);s=state["sessions"][0]
            await runtime.physical.output.drain()
            self.assertEqual(runtime.physical._adapters["a"].transport.frames[-1].target,
                             LightTarget(*(linear_channel(v) for v in [255,100,0]),.5))
            with self.assertRaises(Conflict):
                await runtime.command(dict(op="start",request_id="other",zone_id="other",devices=["a"],effect={"kind":"static"}))
            adjust=dict(op="calibration_adjust",session_id=s["id"],revision=s["revision"],targets=start["targets"])
            new=await runtime.command(adjust)
            with self.assertRaises(StaleRevision): await runtime.command(adjust)
            await runtime.command(dict(op="stop",session_id=s["id"],revision=new["sessions"][0]["revision"]))
            self.assertEqual(auth.states,{"a":ORIGINAL,"b":ORIGINAL})
            normal=await runtime.command(dict(op="start",request_id="normal",zone_id="normal",devices=["a"],effect={"kind":"static","color":[linear_channel(v) for v in [255,100,0]],"brightness":.5}))
            self.assertEqual(normal["targets"]["a"]["target"]["brightness"],.4)
            with self.assertRaises(ValueError): await runtime.command({**cmd,"revision":1})
        finally: await runtime.close()
