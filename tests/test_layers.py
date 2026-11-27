"""Original effect/session/library paths, no physical device I/O."""
import unittest
from engine.lightcadence_engine.effects import decode_effect, LightTarget
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.library import effect
from engine.lightcadence_engine.sessions import Conflict


def leaf(kind="static", color=(1, 0, 0), brightness=1):
    return dict(kind=kind, period_seconds=4, brightness=brightness, color=list(color), width=.25)


def layer(kind="static", color=(1, 0, 0), brightness=1, opacity=1, blend="over"):
    return dict(effect=leaf(kind, color, brightness), opacity=opacity, blend=blend)


def stack(*layers, **params):
    return {**leaf("layered"), "layers": list(layers), **params}


class LayerTests(unittest.TestCase):
    def test_linear_blend_order_and_ambient(self):
        blue = layer(color=(0, 0, 1))
        red = layer(opacity=.5)
        spec = decode_effect(stack(blue, red))
        self.assertEqual(spec.render(0, 0), LightTarget(1, 0, 1, .5))
        self.assertEqual(decode_effect(stack(red, blue)).render(0, 0), LightTarget(0, 0, 1, 1))
        scanner = layer("scanner", opacity=1)
        self.assertEqual(decode_effect(stack(blue, scanner)).render(0, 1), LightTarget(0, 0, 1, 1))
        for mode in ("add", "max"):
            self.assertEqual(decode_effect(stack(blue, layer(blend=mode))).render(0, 0),
                             LightTarget(1, 0, 1, 1))

    def test_preset_roundtrip_audio_and_invalid_stack(self):
        raw = stack(layer(), layer("audio_spectrum", blend="add"))
        self.assertEqual(effect(raw), raw)
        with self.assertRaises(ValueError):
            decode_effect(raw)
        spec = decode_effect({**raw, "audio_source": "test"})
        self.assertEqual(spec.audio_source, "test")
        self.assertEqual(spec.layers[1].effect.audio_source, "test")
        for bad in (stack(), stack(layer(opacity=True)), stack(layer(blend="invalid")),
                    stack(dict(effect=raw, opacity=1, blend="over"))):
            with self.assertRaises(ValueError):
                effect(bad)

    def test_same_manager_conflict_and_single_frame_per_lamp(self):
        rt = PreviewRuntime(clock=lambda: 100.)
        raw = stack(layer(color=(0, 0, 1)), layer("scanner", opacity=.5))
        command = dict(op="start", request_id="r", zone_id="z", devices=["a", "b"], effect=raw)
        state = rt.command(command)
        self.assertEqual(len(state["targets"]), 2)
        self.assertEqual(state["targets"]["b"]["target"]["blue"], 1.)
        self.assertEqual(state["targets"]["a"]["target"]["red"], 1.)
        self.assertEqual(len(rt.command(command)["sessions"]), 1)
        with self.assertRaises(Conflict):
            rt.command({**command, "request_id": "other"})
        s = state["sessions"][0]
        after = rt.command(dict(op="detach", session_id=s["id"], revision=s["revision"], device_id="a"))
        self.assertEqual(set(after["targets"]), {"b"})
