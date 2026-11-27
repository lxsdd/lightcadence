import math
import unittest

from engine.lightcadence_engine.effects import Effect, EffectKind, LightTarget
from engine.lightcadence_engine.sessions import (
    Conflict, DeviceState, Phase, SessionError, SessionManager, StaleRevision, StopPolicy,
)

ORIGINAL = DeviceState(True, LightTarget(0.2, 0.3, 0.4, 0.5))


class EffectTests(unittest.TestCase):
    def test_rainbow_period_and_spatial_phase(self):
        effect = Effect(EffectKind.RAINBOW, period_seconds=4)
        self.assertEqual(effect.render(0, 0), effect.render(4, 0))
        self.assertNotEqual(effect.render(0, 0), effect.render(0, 0.5))
        self.assertNotEqual(effect.render(0, 0), effect.render(1, 0))

    def test_scanner_and_pulse(self):
        scanner = Effect(EffectKind.SCANNER, period_seconds=4)
        self.assertEqual(scanner.render(0, 0).brightness, 1)
        self.assertEqual(scanner.render(0, 1).brightness, 0)
        self.assertEqual(scanner.render(2, 1).brightness, 1)
        pulse = Effect(EffectKind.PULSE, period_seconds=4, brightness=0.8)
        self.assertEqual(pulse.render(0, 0).brightness, 0)
        self.assertAlmostEqual(pulse.render(2, 0).brightness, 0.8)

    def test_invalid_values_are_rejected(self):
        for invalid in (0, -1, math.nan, math.inf, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Effect(EffectKind.PULSE, period_seconds=invalid)
        with self.assertRaises(ValueError):
            LightTarget(1, 1, 1, math.nan)
        with self.assertRaises(ValueError):
            Effect(EffectKind.PULSE).render(-1, 0)
        with self.assertRaises(ValueError):
            Effect(EffectKind.PULSE).render(0, 1.1)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.manager = SessionManager(clock=lambda: 100.0)
        self.effect = Effect(EffectKind.RAINBOW)

    def start(self, name, devices, policy=StopPolicy.RESTORE):
        return self.manager.start(name, name, {d: ORIGINAL for d in devices},
                                  self.effect, policy)

    def test_independent_zones_and_atomic_conflict(self):
        a = self.start("a", ["a1", "a2"])
        b = self.start("b", ["b1"])
        with self.assertRaises(Conflict) as caught:
            self.start("c", ["free", "a2"])
        self.assertEqual(caught.exception.owners, {"a2": a.id})
        self.assertIsNone(self.manager.owner("free"))
        self.assertEqual(self.manager.render(a.id, 101), 2)
        self.assertEqual(self.manager.render(b.id, 102), 1)
        self.assertEqual(self.manager.take_latest("b1").session_id, b.id)

    def test_idempotent_reconnect_and_stale_revision(self):
        a = self.start("a", ["a1", "a2"])
        self.assertEqual(self.start("a", ["a1", "a2"]), a)
        with self.assertRaises(SessionError):
            self.start("a", ["a1"])
        new = self.manager.detach(a.id, a.revision, "a1")
        self.assertEqual(self.start("a", ["a1", "a2"]), new)
        with self.assertRaises(StaleRevision):
            self.manager.begin_stop(a.id, a.revision)
        self.assertEqual(len(self.manager.snapshots()), 1)

    def test_latest_value_and_monotonic_time(self):
        a = self.start("a", ["a1"])
        self.manager.render(a.id, 101)
        old = self.manager.take_latest("a1")
        for at in (102, 103, 104):
            self.manager.render(a.id, at)
        current = self.manager.take_latest("a1")
        self.assertFalse(self.manager.frame_is_current(old))
        self.assertTrue(self.manager.frame_is_current(current))
        self.assertEqual(current.target_time, 104)
        self.assertIsNone(self.manager.take_latest("a1"))
        with self.assertRaises(SessionError):
            self.manager.render(a.id, 103)

    def test_manual_detach_invalidates_frame_and_excludes_restore(self):
        a = self.start("a", ["a1", "a2"])
        self.manager.render(a.id, 101)
        old = self.manager.take_latest("a1")
        a = self.manager.detach(a.id, a.revision, "a1")
        self.assertFalse(self.manager.frame_is_current(old))
        b = self.start("b", ["a1"])
        intents = self.manager.begin_stop(a.id, a.revision)
        self.assertEqual([i.device_id for i in intents], ["a2"])
        self.assertEqual(intents[0].state, ORIGINAL)
        s = self.manager.acknowledge_stop(intents[0], success=True)
        self.manager.finish_stop(a.id, s.revision)
        self.assertEqual(self.manager.owner("a1"), b.id)
        self.assertEqual(self.manager.snapshot(b.id).phase, Phase.RUNNING)

    def test_stop_rejects_stale_frame_and_unconfirmed_restore(self):
        a = self.start("a", ["a1"])
        self.manager.render(a.id, 101)
        frame = self.manager.take_latest("a1")
        intent, = self.manager.begin_stop(a.id, a.revision)
        self.assertFalse(self.manager.frame_is_current(frame))
        self.assertEqual(self.manager.render(a.id, 102), 0)
        self.assertTrue(self.manager.stop_intent_is_current(intent))
        s = self.manager.acknowledge_stop(intent, success=False)
        with self.assertRaises(SessionError):
            self.manager.finish_stop(a.id, s.revision)
        with self.assertRaises(Conflict):
            self.start("b", ["a1"])
        s = self.manager.acknowledge_stop(intent, success=True)
        self.assertFalse(self.manager.stop_intent_is_current(intent))
        s = self.manager.finish_stop(a.id, s.revision)
        self.assertEqual(s.phase, Phase.STOPPED)
        self.assertIsNone(self.manager.owner("a1"))

    def test_detach_during_stop_and_new_owner_fences_old_intent(self):
        a = self.start("a", ["a1", "a2"])
        intents = self.manager.begin_stop(a.id, a.revision)
        s = self.manager.snapshot(a.id)
        self.manager.detach(a.id, s.revision, "a1")
        b = self.start("b", ["a1"])
        self.assertFalse(self.manager.stop_intent_is_current(intents[0]))
        with self.assertRaises(SessionError):
            self.manager.acknowledge_stop(intents[0], success=True)
        s = self.manager.acknowledge_stop(intents[1], success=True)
        self.manager.finish_stop(a.id, s.revision)
        self.assertEqual(self.manager.owner("a1"), b.id)

    def test_off_and_hold(self):
        a = self.start("a", ["a1"], StopPolicy.OFF)
        intent, = self.manager.begin_stop(a.id, a.revision)
        self.assertFalse(intent.state.power)
        s = self.manager.acknowledge_stop(intent, success=True)
        self.manager.finish_stop(a.id, s.revision)
        b = self.start("b", ["a1"], StopPolicy.HOLD)
        self.assertEqual(self.manager.begin_stop(b.id, b.revision), ())
        self.assertEqual(self.manager.owner("a1"), b.id)
        s = self.manager.snapshot(b.id)
        self.manager.finish_stop(b.id, s.revision)
        self.assertIsNone(self.manager.owner("a1"))

    def test_no_artificial_participant_cap(self):
        for count in (15, 32, 64, 128):
            with self.subTest(count=count):
                devices = [f"{count}-{i}" for i in range(count)]
                a = self.start(str(count), devices, StopPolicy.HOLD)
                self.assertEqual(self.manager.render(a.id, 101), count)
                for d in devices:
                    self.assertTrue(self.manager.frame_is_current(self.manager.take_latest(d)))
                self.manager.begin_stop(a.id, a.revision)
                s = self.manager.snapshot(a.id)
                self.manager.finish_stop(a.id, s.revision)

    def test_bad_selection_does_not_reserve_devices(self):
        with self.assertRaises(SessionError):
            self.manager.start("r", "z", {"a": ORIGINAL}, self.effect,
                               positions={"other": 0})
        self.assertIsNone(self.manager.owner("a"))
        with self.assertRaises(SessionError):
            self.start("empty", [])
        self.assertEqual(self.manager.snapshots(), ())
