"""Original enrollment, lease, UDP and restore paths with synthetic authority."""
import asyncio
import json
import socket
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from engine.lightcadence_engine.effects import Effect, EffectKind, LightTarget
from engine.lightcadence_engine.enrollment import ChromaProfile, Observation, PhysicalSessions
from engine.lightcadence_engine.sessions import Conflict, DeviceState, Phase, SessionError, StopPolicy
from engine.lightcadence_engine.recovery import RecoveryJournal

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


    async def test_manual_context_holds_reservation_and_never_restores(self):
        session = await self.start()
        async with self.runtime.manual_handover("lamp"):
            self.assertIsNone(self.runtime.manager.owner("lamp"))
            self.assertFalse(self.runtime._adapters["lamp"].transport.ready)
            with self.assertRaises(Conflict):
                await self.runtime.start("new", "zone", ["lamp"], EFFECT)
            with self.assertRaises(Conflict):
                async with self.runtime.manual_handover("lamp"):
                    self.fail("concurrent manual writer granted")
            self.state.states["lamp"] = CHANGED
        stopped = await self.runtime.stop(session.id, self.runtime.manager.snapshot(session.id).revision)
        self.assertEqual(stopped.phase, Phase.STOPPED)
        self.assertEqual(self.state.writes, [])
        self.assertEqual(self.state.states["lamp"], CHANGED)
        self.assertEqual((await self.start("new")).phase, Phase.RUNNING)

    async def test_manual_context_releases_on_exception_but_not_during_admission(self):
        self.state.gate.clear()
        task = asyncio.create_task(self.start())
        await self.state.entered.wait()
        with self.assertRaises(Conflict):
            async with self.runtime.manual_handover("lamp"):
                self.fail("admission was interrupted")
        self.state.gate.set()
        await task
        with self.assertRaisesRegex(RuntimeError, "manual failure"):
            async with self.runtime.manual_handover("lamp"):
                raise RuntimeError("manual failure")
        self.assertEqual((await self.start("new")).phase, Phase.RUNNING)

    async def test_parallel_acquisition_and_atomic_failure(self):
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second.bind(("127.0.0.1", 0))
        second.setblocking(False)
        self.addCleanup(second.close)
        p2 = replace(self.p, device_id="other", ha_registry_id="other-ha",
                     port=second.getsockname()[1])
        batch = PhysicalSessions([self.p, p2], self.state, enabled=True)
        for adapter in batch._adapters.values():
            self.addCleanup(adapter.transport.close)
        loop = asyncio.get_running_loop()
        async def receive_both():
            async with asyncio.timeout(.8):
                return await asyncio.gather(loop.sock_recvfrom(self.peer, 4096),
                                            loop.sock_recvfrom(second, 4096))
        pending = asyncio.create_task(batch.start("batch", "zone", ["lamp", "other"], EFFECT))
        first, other = await receive_both()
        # Both requests arrived before either peer responded.
        for peer, (data, addr), success in ((self.peer, first, True), (second, other, False)):
            msg = json.loads(data)
            self.assertEqual(msg["method"], "udp_sess_new")
            reply = {"id": msg["id"], "method": "udp_sess_token",
                     "params": {"token": "a" * 32}} if success else {"id": msg["id"], "error": {"code": -1}}
            peer.sendto(json.dumps(reply).encode(), addr)
        with self.assertRaises(OSError):
            await pending
        self.assertEqual(batch.manager.snapshots(), ())
        self.assertTrue(all(not a.transport.ready for a in batch._adapters.values()))
        pending = asyncio.create_task(batch.start("batch", "zone", ["lamp", "other"], EFFECT))
        first, other = await receive_both()
        for peer, (data, addr) in ((self.peer, first), (second, other)):
            msg = json.loads(data)
            peer.sendto(json.dumps({"id": msg["id"], "method": "udp_sess_token",
                                     "params": {"token": "b" * 32}}).encode(), addr)
        self.assertEqual((await pending).devices, ("lamp", "other"))

    async def test_parallel_reads_finish_before_any_acquisition(self):
        p2 = replace(self.p, device_id="other", ha_registry_id="other-ha",
                     port=12345 if self.p.port != 12345 else 12346)
        batch = PhysicalSessions([self.p, p2], self.state, enabled=True)
        self.state.gate.clear()
        self.state.mode = "ct"
        pending = asyncio.create_task(batch.start("batch", "zone", ["lamp", "other"], EFFECT))
        await self.state.entered.wait()
        await asyncio.sleep(0)
        self.assertCountEqual(self.state.reads, ["lamp", "other"])
        self.state.gate.set()
        with self.assertRaises(SessionError):
            await pending
        self.assertTrue(all(a.transport._socket is None for a in batch._adapters.values()))
        self.assertEqual(batch.manager.snapshots(), ())

    def durable(self, directory, binding="binding"):
        journal = RecoveryJournal(directory, {"lamp": binding})
        self.addCleanup(journal.close)
        self.runtime = PhysicalSessions([self.p], self.state, enabled=True, journal=journal)
        self.addCleanup(self.runtime._adapters["lamp"].transport.close)
        return journal

    async def test_journal_before_udp_restart_and_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.durable(directory)
            task = asyncio.create_task(self.runtime.start("r", "zone", ["lamp"], EFFECT))
            msg, address = await self.receive()
            disk = json.loads((Path(directory) / "recovery-v1.json").read_text())
            self.assertEqual(disk["records"]["lamp"]["original"]["power"], True)
            self.peer.sendto(json.dumps({"id": msg["id"], "method": "udp_sess_token",
                                         "params": {"token": "a" * 32}}).encode(), address)
            await task
            await self.runtime.close()
            self.state.states["lamp"] = CHANGED
            journal = self.durable(directory)
            self.assertEqual(self.runtime.manager.snapshots(), ())
            self.assertEqual(self.state.writes, [])
            self.assertEqual(journal.recovery, {"lamp"})
            with self.assertRaises(Conflict):
                await self.runtime.start("new", "zone", ["lamp"], EFFECT)
            with self.assertRaises(Conflict):
                await self.runtime.manual_off("lamp")
            record = journal.snapshot()[0]["record_id"]
            self.assertEqual(await self.runtime.recover("lamp", record, "restore"), "confirmed")
            self.assertEqual(self.state.writes, [("lamp", ORIGINAL)])
            self.assertEqual(journal.snapshot(), [])
            with self.assertRaises(ValueError):
                await self.runtime.recover("lamp", record, "restore")
            self.assertEqual(len(self.state.writes), 1)
            await self.runtime.close()

    async def test_recovery_identity_mismatch_and_uncertain_attempt_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.durable(directory)
            journal.add({"lamp": ORIGINAL}, StopPolicy.RESTORE)
            await self.runtime.close()
            journal = self.durable(directory, "changed-binding")
            record = journal.snapshot()[0]["record_id"]
            self.assertFalse(journal.snapshot()[0]["binding_matches"])
            with self.assertRaises(ValueError):
                await self.runtime.recover("lamp", record, "restore")
            self.assertEqual(self.state.writes, [])
            await self.runtime.close()
            journal = self.durable(directory)
            self.state.states["lamp"] = CHANGED
            self.state.apply = False
            self.assertEqual(await self.runtime.recover("lamp", record, "restore"), "unconfirmed")
            with self.assertRaises(ValueError):
                await self.runtime.recover("lamp", record, "restore")
            self.assertEqual(len(self.state.writes), 1)
            current = journal.snapshot()[0]["record_id"]
            self.state.wrong_identity = True
            with self.assertRaises(SessionError):
                await self.runtime.recover("lamp", current, "keep")
            self.state.wrong_identity = False
            current = journal.snapshot()[0]["record_id"]
            self.assertEqual(await self.runtime.recover("lamp", current, "keep"), "confirmed")
            self.assertEqual(self.state.states["lamp"], CHANGED)
            self.assertEqual(len(self.state.writes), 1)
            await self.runtime.close()

    async def test_manual_detach_clears_durable_intent_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.durable(directory)
            await self.start()
            async with self.runtime.manual_handover("lamp"):
                self.assertEqual(journal.records, {})
                self.assertIsNone(self.runtime.manager.owner("lamp"))
                self.state.states["lamp"] = CHANGED
            await self.runtime.close()
            journal = self.durable(directory)
            self.assertEqual(journal.snapshot(), [])
            self.assertEqual(self.state.writes, [])
            await self.runtime.close()

    async def test_failed_journal_write_prevents_udp_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.durable(directory)
            with patch("engine.lightcadence_engine.recovery.os.replace", side_effect=OSError("synthetic disk fault")):
                with self.assertRaises(OSError):
                    await self.runtime.start("r", "zone", ["lamp"], EFFECT)
            self.assertTrue(journal.fault)
            self.assertEqual(self.runtime.manager.snapshots(), ())
            with self.assertRaises(OSError):
                await self.runtime.manual_off("lamp")
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(.02):
                    await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
            await self.runtime.close()

    async def test_journal_lock_and_corrupt_restart_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.durable(directory)
            with self.assertRaises(OSError):
                RecoveryJournal(directory, {"lamp": "binding"})
            await self.runtime.close()
            (Path(directory) / "recovery-v1.json").write_text('{"schema_version":99}', encoding="utf-8")
            with self.assertRaises(ValueError):
                RecoveryJournal(directory, {"lamp": "binding"})
