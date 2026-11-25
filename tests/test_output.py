"""Original coordinator and SessionManager regressions; no device or network I/O."""
import asyncio
import unittest

from engine.lightcadence_engine.effects import Effect, EffectKind, LightTarget
from engine.lightcadence_engine.output import DeviceOutput, OutputCoordinator
from engine.lightcadence_engine.sessions import (
    Conflict, DeviceState, Phase, SessionManager, StopPolicy,
)


class RecordingAdapter:
    def __init__(self):
        self.frames = []
        self.stops = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.confirm = True
        self.fail = False

    async def send_frame(self, frame):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise OSError("synthetic transport error")
        self.frames.append(frame)

    async def apply_stop(self, intent):
        self.stops.append(intent)
        return self.confirm


class OutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.at = 100.0
        self.manager = SessionManager(lambda: self.at)
        self.a, self.b = RecordingAdapter(), RecordingAdapter()
        self.output = OutputCoordinator(self.manager, {
            "a": DeviceOutput(self.a, 0.04),
            "b": DeviceOutput(self.b, 0),
        }, lambda: self.at)

    def start(self, name, devices, policy=StopPolicy.RESTORE):
        return self.manager.start(name, name,
            {d: DeviceState(True, LightTarget(.1, .2, .3, .4)) for d in devices},
            Effect(EffectKind.RAINBOW), policy)

    def tick(self, session, at):
        self.at = at
        self.manager.render(session.id, at)
        self.output.pump()

    async def test_slow_device_never_blocks_other_or_replays_backlog(self):
        s = self.start("show", ["a", "b"])
        self.a.release.clear()
        self.tick(s, 101)
        await self.a.entered.wait()
        await asyncio.sleep(0)
        for at in (102, 103, 104):
            self.tick(s, at)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertEqual([f.target_time for f in self.b.frames], [101, 102, 103, 104])
        self.assertEqual(self.a.frames, [])
        self.a.release.set()
        await self.output.drain()
        self.output.pump()
        await self.output.drain()
        self.assertEqual([f.target_time for f in self.a.frames], [101, 104])
        self.assertEqual(self.output.status["a"].outcome, "submitted_unverified")

    async def test_device_specific_cadence_preserves_latest(self):
        s = self.start("show", ["a", "b"])
        self.tick(s, 101)
        await self.output.drain()
        self.tick(s, 101.01)
        await self.output.drain()
        self.tick(s, 101.02)
        await self.output.drain()
        self.assertEqual(len(self.a.frames), 1)
        self.assertEqual(len(self.b.frames), 3)
        self.at = 101.05
        self.output.pump()
        await self.output.drain()
        self.assertEqual(self.a.frames[-1].target_time, 101.02)

    async def test_handover_waits_for_inflight_and_excludes_restore(self):
        s = self.start("show", ["a", "b"])
        self.a.release.clear()
        self.tick(s, 101)
        await self.a.entered.wait()
        handover = asyncio.create_task(self.output.detach(s.id, s.revision, "a"))
        await asyncio.sleep(0)
        self.assertFalse(handover.done())
        self.tick(s, 102)
        with self.assertRaises(Conflict):
            self.start("other", ["a"])
        self.a.release.set()
        detached = await handover
        await self.output.drain()
        self.assertEqual(len(self.a.frames), 1)
        self.assertIsNone(self.manager.owner("a"))
        other = self.start("other", ["a"])
        result = await self.output.stop(s.id, detached.revision)
        self.assertEqual(result.phase, Phase.STOPPED)
        self.assertEqual(self.a.stops, [])
        self.assertEqual(len(self.b.stops), 1)
        self.assertEqual(self.manager.owner("a"), other.id)

    async def test_stop_fences_queued_frames_and_retries_unconfirmed_restore(self):
        s = self.start("show", ["a", "b"])
        self.a.release.clear()
        self.a.confirm = False
        self.tick(s, 101)
        await self.a.entered.wait()
        self.tick(s, 102)
        stop = asyncio.create_task(self.output.stop(s.id, s.revision))
        await asyncio.sleep(0)
        self.assertEqual(self.manager.snapshot(s.id).phase, Phase.STOPPING)
        self.assertFalse(stop.done())
        self.a.release.set()
        pending = await stop
        self.assertEqual(len(self.a.frames), 1)
        self.assertEqual(pending.phase, Phase.STOPPING)
        self.assertEqual(self.output.status["a"].outcome, "stop_unconfirmed")
        with self.assertRaises(Conflict):
            self.start("other", ["a"])
        self.a.confirm = True
        result = await self.output.stop(s.id, pending.revision)
        self.assertEqual(result.phase, Phase.STOPPED)
        self.assertEqual(len(self.a.stops), 2)
        self.assertEqual(len(self.b.stops), 1)
        self.assertIsNone(self.manager.owner("a"))

    async def test_hold_waits_for_inflight_before_new_owner(self):
        s = self.start("show", ["a"], StopPolicy.HOLD)
        self.a.release.clear()
        self.tick(s, 101)
        await self.a.entered.wait()
        stop = asyncio.create_task(self.output.stop(s.id, s.revision))
        await asyncio.sleep(0)
        with self.assertRaises(Conflict):
            self.start("other", ["a"])
        self.a.release.set()
        self.assertEqual((await stop).phase, Phase.STOPPED)
        self.assertEqual(self.a.stops, [])
        self.assertIsNotNone(self.start("other", ["a"]))

    async def test_missing_adapter_and_failed_send_are_not_success(self):
        s = self.start("show", ["a", "missing"])
        self.a.fail = True
        self.tick(s, 101)
        await self.output.drain()
        self.assertEqual(self.output.status["a"].outcome, "transport_error")
        result = await self.output.stop(s.id, s.revision)
        self.assertEqual(result.phase, Phase.STOPPING)
        self.assertEqual(self.manager.owner("missing"), s.id)
        self.assertNotIn("missing", self.output.status)

    async def test_stale_detach_never_closes_transport(self):
        calls = []
        self.output.devices["a"] = DeviceOutput(self.a, 0, release=lambda: calls.append("closed"))
        s = self.start("show", ["a"], StopPolicy.HOLD)
        with self.assertRaises(ValueError):
            await self.output.detach(s.id, s.revision + 1, "a")
        self.assertEqual(calls, [])
        self.assertEqual(self.manager.owner("a"), s.id)
        await self.output.detach(s.id, s.revision, "a")
        self.assertEqual(calls, ["closed"])
        self.assertIsNone(self.manager.owner("a"))

    async def test_failed_handover_cleanup_fences_frames_until_retry(self):
        calls = []
        def close():
            calls.append("close")
            if len(calls) == 1:
                raise OSError("synthetic cleanup failure")
        self.output.devices["a"] = DeviceOutput(self.a, 0, release=close)
        s = self.start("show", ["a", "b"], StopPolicy.HOLD)
        with self.assertRaises(ValueError):
            await self.output.detach(s.id, s.revision, "a")
        self.assertEqual(self.manager.owner("a"), s.id)
        self.assertEqual(self.output.status["a"].outcome, "cleanup_error")
        self.tick(s, 101)
        await self.output.drain()
        self.assertEqual(self.a.frames, [])
        self.assertEqual(len(self.b.frames), 1)
        with self.assertRaises(Conflict):
            self.start("other", ["a"])
        await self.output.detach(s.id, s.revision, "a")
        self.assertIsNone(self.manager.owner("a"))
        self.assertEqual(calls, ["close", "close"])

    async def test_confirmed_restore_is_not_repeated_after_cleanup_failure(self):
        calls = []
        def close():
            calls.append("close")
            if len(calls) == 1:
                raise OSError("synthetic cleanup failure")
        self.output.devices["a"] = DeviceOutput(self.a, 0, release=close)
        s = self.start("show", ["a"])
        pending = await self.output.stop(s.id, s.revision)
        self.assertEqual(pending.phase, Phase.STOPPING)
        self.assertEqual(self.output.status["a"].outcome, "cleanup_error")
        self.assertEqual(self.manager.owner("a"), s.id)
        stopped = await self.output.stop(s.id, pending.revision)
        self.assertEqual(stopped.phase, Phase.STOPPED)
        self.assertEqual(len(self.a.stops), 1)
        self.assertEqual(calls, ["close", "close"])
        self.assertIsNone(self.manager.owner("a"))

    async def test_hold_cleanup_failure_keeps_lease(self):
        calls = []
        def close():
            calls.append("close")
            if len(calls) == 1:
                raise OSError("synthetic cleanup failure")
        self.output.devices["a"] = DeviceOutput(self.a, 0, release=close)
        s = self.start("show", ["a"], StopPolicy.HOLD)
        pending = await self.output.stop(s.id, s.revision)
        self.assertEqual(pending.phase, Phase.STOPPING)
        self.assertEqual(self.manager.owner("a"), s.id)
        self.assertEqual(self.a.stops, [])
        self.assertEqual((await self.output.stop(s.id, pending.revision)).phase, Phase.STOPPED)
        self.assertIsNone(self.manager.owner("a"))
