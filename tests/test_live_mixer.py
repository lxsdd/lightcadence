"""Live mixing uses original renderer, sessions and authenticated HTTP."""
import unittest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from engine.lightcadence_engine.effects import Effect, EffectKind, Layer, Blend, Track, Clip, LightTarget
from engine.lightcadence_engine.sessions import SessionManager, DeviceState, StopPolicy, StaleRevision, SessionError
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app

RED = Effect(EffectKind.STATIC, color=(1., 0., 0.))
BLUE = Effect(EffectKind.STATIC, color=(0., 0., 1.))
ORIGINAL = DeviceState(True, LightTarget(0., 1., 0., .5))


class MixerTests(unittest.IsolatedAsyncioTestCase):
    def test_group_master_solo_mute_detach_and_other_zone(self):
        manager = SessionManager(lambda: 10.)
        effect = Effect(EffectKind.LAYERED, layers=(
            Layer(RED, devices=("a", "b")), Layer(BLUE, blend=Blend.ADD, devices=("a",))))
        s = manager.start("start", "zone", {"a": ORIGINAL, "b": ORIGINAL}, effect)
        other = manager.start("other", "other", {"c": ORIGINAL}, RED)
        manager.render(s.id)
        old = manager.peek_latest("a")
        s = manager.mix_control(s.id, s.revision, "gain", .25, 1)
        self.assertFalse(manager.frame_is_current(old))
        manager.render(s.id)
        self.assertEqual(manager.peek_latest("a").target, LightTarget(1., 0., .25, 1.))
        s = manager.mix_control(s.id, s.revision, "solo", True, 1)
        s = manager.mix_control(s.id, s.revision, "master", .5)
        manager.render(s.id)
        self.assertEqual(manager.peek_latest("a").target, LightTarget(0., 0., 1., .125))
        self.assertEqual(manager.peek_latest("b").target.brightness, 0.)
        s = manager.mix_control(s.id, s.revision, "mute", True, 1)
        manager.render(s.id)
        self.assertEqual(manager.peek_latest("a").target.brightness, 0.)
        s = manager.detach(s.id, s.revision, "a")
        s = manager.mix_control(s.id, s.revision, "solo", False, 1)
        manager.render(s.id)
        self.assertIsNone(manager.owner("a"))
        self.assertIsNone(manager.peek_latest("a"))
        self.assertEqual(manager.owner("c"), other.id)
        self.assertEqual(manager.snapshot(other.id), other)
        self.assertEqual(s.effect, effect)
        intents = manager.begin_stop(s.id, s.revision)
        self.assertEqual([(x.device_id, x.state) for x in intents], [("b", ORIGINAL)])

    def test_timeline_mix_retains_pause_playhead_and_rejects_invalid(self):
        clock = [10.]
        manager = SessionManager(lambda: clock[0])
        effect = Effect(EffectKind.TIMELINE, tracks=(Track("base", (Clip(0., 8., Layer(RED)),)),))
        s = manager.start("start", "zone", {"a": ORIGINAL}, effect)
        clock[0] = 12.
        s = manager.timeline_control(s.id, s.revision, "pause")
        for action, value, index in (("gain", True, 0), ("gain", .2, 1), ("solo", 1, 0),
                                      ("master", float("nan"), None), ("master", .2, 0), ("bad", 0, None)):
            with self.assertRaises(SessionError):
                manager.mix_control(s.id, s.revision, action, value, index)
        self.assertEqual(manager.snapshot(s.id), s)
        with self.assertRaises(StaleRevision):
            manager.mix_control(s.id, s.revision - 1, "gain", .5, 0)
        s = manager.mix_control(s.id, s.revision, "gain", .5, 0)
        clock[0] = 20.
        self.assertEqual(manager.timeline_state(s.id)["playhead"], 2.)
        manager.render(s.id)
        self.assertEqual(manager.peek_latest("a").target, LightTarget(1., 0., 0., .5))
        manager.begin_stop(s.id, s.revision)
        with self.assertRaises(SessionError):
            manager.mix_control(s.id, manager.snapshot(s.id).revision, "master", .1)

    async def test_http_auth_revision_instance_and_no_preset_edit(self):
        runtime = PreviewRuntime()
        token = "x" * 40
        async with TestServer(create_app(token, runtime)) as server, ClientSession() as client:
            url = server.make_url("/v1/preview")
            headers = {"Authorization": "Bearer " + token}
            async with client.post(url, headers=headers, json={"op":"start", "request_id":"x", "zone_id":"z", "devices":["a"], "effect":{"kind":"static"}}) as response:
                self.assertEqual(response.status, 200)
                session = (await response.json())["sessions"][0]
            body = dict(op="mix_control", instance_id=runtime.instance_id, session_id=session["id"], revision=session["revision"], action="master", value=.3)
            async with client.post(url, json=body) as response:
                self.assertEqual(response.status, 401)
            async with client.post(url, headers=headers, json={**body, "instance_id":"old"}) as response:
                self.assertEqual(response.status, 409)
            async with client.post(url, headers=headers, json=body) as response:
                self.assertEqual(response.status, 200)
                state = await response.json()
            self.assertEqual(state["sessions"][0]["mix"]["master"], .3)
            self.assertEqual(state["targets"]["a"]["target"]["brightness"], .3)
            self.assertEqual(state["library"]["revision"], 0)
            async with client.post(url, headers=headers, json=body) as response:
                self.assertEqual(response.status, 409)
