"""Original enrollment, lease, UDP and restore paths with synthetic authority."""
import asyncio
import json
import socket
import unittest
from dataclasses import replace

from engine.lightcadence_engine.effects import Effect, EffectKind, LightTarget
from engine.lightcadence_engine.enrollment import ChromaProfile, Observation, PhysicalSessions
from engine.lightcadence_engine.sessions import Conflict, DeviceState, Phase, SessionError

ORIGINAL = DeviceState(True, LightTarget(.5, .2, .1, .6))
CHANGED = DeviceState(True, LightTarget(0, 1, 0, .8))
EFFECT = Effect(EffectKind.RAINBOW)


class SyntheticState:
    def __init__(self):
        self.states = {}
        self.writes = []
        self.reads = []
        self.confirm = True
        self.apply = True
        self.mode = "rgb"
        self.wrong_identity = False
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()

    async def read(self, p):
        self.reads.append(p.device_id)
        self.entered.set()
        await self.gate.wait()
        return Observation("wrong" if self.wrong_identity else p.device_id,
                           p.ha_registry_id, p.model, p.firmware, self.mode,
                           self.states.get(p.device_id, ORIGINAL))

    async def write(self, p, state):
        self.writes.append((p.device_id, state))
        if self.apply:
            self.states[p.device_id] = state
        return self.confirm


class EnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.peer.bind(("127.0.0.1", 0))
        self.peer.setblocking(False)
        self.addCleanup(self.peer.close)
        self.p = ChromaProfile("lamp", "ha-id", "synthetic", "test",
                               self.peer.getsockname()[0], 0, self.peer.getsockname()[1])
        self.state = SyntheticState()
        self.runtime = PhysicalSessions([self.p], self.state, enabled=True)
        self.addCleanup(self.runtime._adapters["lamp"].transport.close)

    async def receive(self):
        async with asyncio.timeout(1):
            data, address = await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
        return json.loads(data), address

    async def start(self, request="request"):
        pending = asyncio.create_task(self.runtime.start(request, "zone", ["lamp"], EFFECT))
        msg, address = await self.receive()
        self.assertEqual(msg["method"], "udp_sess_new")
        self.peer.sendto(json.dumps({"id": msg["id"], "method": "udp_sess_token",
                                      "params": {"token": "a" * 32}}).encode(), address)
        return await pending

    async def test_profile_identity_opt_in_and_duplicate_endpoints(self):
        with self.assertRaises(ValueError):
            PhysicalSessions([self.p], self.state)
        with self.assertRaises(ValueError):
            PhysicalSessions([self.p, replace(self.p, device_id="other", ha_registry_id="other")],
                             self.state, enabled=True)
        with self.assertRaises(ValueError):
            PhysicalSessions([self.p, replace(self.p, device_id="other", port=1234)],
                             self.state, enabled=True)
        self.assertEqual(self.state.reads, [])

    async def test_capture_then_real_udp_and_confirmed_restore(self):
        session = await self.start()
        self.assertEqual(self.state.reads, ["lamp"])
        self.runtime.tick()
        await self.runtime.output.drain()
        msg, _ = await self.receive()
        self.assertEqual(msg["method"], "set_scene")
        self.assertEqual(self.runtime.output.status["lamp"].outcome, "submitted_unverified")
        self.state.states["lamp"] = CHANGED
        result = await self.runtime.stop(session.id, session.revision)
        self.assertEqual(result.phase, Phase.STOPPED)
        self.assertEqual(self.state.writes, [("lamp", ORIGINAL)])
        self.assertEqual(self.state.states["lamp"], ORIGINAL)
        self.assertFalse(self.runtime._adapters["lamp"].transport.ready)

    async def test_unconfirmed_readback_keeps_lease_and_explicit_retry(self):
        session = await self.start()
        self.state.states["lamp"] = CHANGED
        self.state.apply = False
        pending = await self.runtime.stop(session.id, session.revision)
        self.assertEqual(pending.phase, Phase.STOPPING)
        self.assertEqual(self.runtime.manager.owner("lamp"), session.id)
        self.assertEqual(self.runtime.output.status["lamp"].outcome, "stop_unconfirmed")
        with self.assertRaises(Conflict):
            await self.runtime.start("other", "other", ["lamp"], EFFECT)
        self.state.apply = True
        stopped = await self.runtime.stop(session.id, pending.revision)
        self.assertEqual(stopped.phase, Phase.STOPPED)

    async def test_invalid_state_never_acquires_and_can_retry(self):
        for mode, wrong in (("ct", False), ("rgb", True)):
            self.state.mode = mode
            self.state.wrong_identity = wrong
            with self.assertRaises(SessionError):
                await self.runtime.start("request", "zone", ["lamp"], EFFECT)
            self.assertEqual(self.runtime.manager.snapshots(), ())
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(.02):
                    await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
        self.state.mode = "rgb"
        self.state.wrong_identity = False
        self.assertEqual((await self.start()).phase, Phase.RUNNING)

    async def test_reservation_during_read_and_idempotent_reconnect(self):
        self.state.gate.clear()
        task = asyncio.create_task(self.start())
        await self.state.entered.wait()
        with self.assertRaises(Conflict):
            await self.runtime.start("other", "other", ["lamp"], EFFECT)
        with self.assertRaises(SessionError):
            await self.runtime.start("request", "zone", ["lamp"], EFFECT)
        self.state.gate.set()
        session = await task
        replay = await self.runtime.start("request", "zone", ["lamp"], EFFECT)
        self.assertEqual(replay.id, session.id)
        self.assertEqual(self.state.reads, ["lamp"])

    async def test_detached_manual_state_never_restored(self):
        session = await self.start()
        detached = await self.runtime.detach(session.id, session.revision, "lamp")
        self.state.states["lamp"] = CHANGED
        self.assertEqual((await self.runtime.stop(session.id, detached.revision)).phase,
                         Phase.STOPPED)
        self.assertEqual(self.state.writes, [])
        self.assertEqual(self.state.states["lamp"], CHANGED)

    async def test_changed_identity_blocks_restore_before_write(self):
        session = await self.start()
        self.state.wrong_identity = True
        pending = await self.runtime.stop(session.id, session.revision)
        self.assertEqual(pending.phase, Phase.STOPPING)
        self.assertEqual(self.state.writes, [])
        self.assertEqual(self.runtime.manager.owner("lamp"), session.id)

    async def test_cancelled_admission_closes_socket_and_clears_reservation(self):
        pending = asyncio.create_task(self.runtime.start("request", "zone", ["lamp"], EFFECT))
        msg, _ = await self.receive()
        self.assertEqual(msg["method"], "udp_sess_new")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.runtime.manager.snapshots(), ())
        self.assertFalse(self.runtime._adapters["lamp"].transport.ready)
        self.assertEqual((await self.start()).phase, Phase.RUNNING)
