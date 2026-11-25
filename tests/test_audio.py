"""Production PCM/DSP/render path, generated tones only; no actual capture."""
import math
import struct
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.audio import Analyzer, AudioHub, FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app

TOKEN = "synthetic_" + "x" * 32
SILENCE = bytes(FRAME_BYTES)


def tone(hz):
    return struct.pack("<" + "h" * FRAME_SAMPLES,
                       *(round(16000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE))
                         for i in range(FRAME_SAMPLES)))


class DspTests(unittest.TestCase):
    def test_fft_separates_three_bands_and_silence(self):
        for band, hz in enumerate((94, 984, 6000)):
            f = Analyzer().analyze(tone(hz), 0.)
            self.assertGreater(f.bands[band], .25)
            self.assertTrue(all(v < .03 for i, v in enumerate(f.bands) if i != band))
            self.assertFalse(f.beat)
        a = Analyzer()
        for i in range(50):
            f = a.analyze(SILENCE, i * .05)
            self.assertEqual(f.bands, (0., 0., 0.))
            self.assertFalse(f.beat)
            self.assertIsNone(f.bpm)

    def test_onsets_and_tempo_not_invented(self):
        a = Analyzer()
        for i in range(5):
            a.analyze(SILENCE, i * .5)
            f = a.analyze(tone(94), i * .5 + .04)
            self.assertTrue(f.beat)
        self.assertAlmostEqual(f.bpm, 120., places=1)
        f = a.analyze(SILENCE, 5.)
        self.assertFalse(f.beat)
        self.assertIsNone(f.bpm)

    def test_sequence_gap_and_loss_reset_analysis(self):
        now = [0.]
        h = AudioHub(lambda: now[0])
        a, b = h.open("A"), h.open("B")
        h.push(a, 0, SILENCE)
        h.push(b, 0, tone(6000))
        now[0] = .04
        h.push(a, 1, tone(94))
        self.assertEqual(h.snapshot()[0]["beat_count"], 1)
        for seq, pcm in ((1, SILENCE), (2, b"x")):
            with self.assertRaises(ValueError):
                h.push(a, seq, pcm)
        self.assertEqual(h.snapshot()[0]["sequence"], 1)
        h.push(a, 9, tone(94))
        self.assertEqual(h.snapshot()[0]["beat_count"], 1)
        self.assertGreater(h.levels(a, now[0])[0], .25)
        self.assertGreater(h.levels(b, now[0])[2], .25)
        now[0] = 2.
        self.assertEqual(h.snapshot()[0]["status"], "lost")
        self.assertEqual(h.levels(a, now[0]), (.06, .06, .06))
        h.close(a)
        self.assertEqual(len(h.snapshot()), 1)


class AudioApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_pcm_reaches_original_session_targets(self):
        now = [0.]
        runtime = PreviewRuntime(lambda: now[0])
        headers = {"Authorization": "Bearer " + TOKEN}
        async with TestServer(create_app(TOKEN, runtime)) as server, ClientSession() as client:
            async def post(path, **kwargs):
                async with client.post(server.make_url(path), **kwargs) as r:
                    return r.status, await r.json()
            payload = {"op": "open", "instance_id": runtime.instance_id, "name": "Synthetic PCM"}
            self.assertEqual((await post("/v1/audio/source", json=payload))[0], 401)
            self.assertEqual((await post("/v1/audio/source", headers=headers,
                                        json={**payload, "instance_id": "old"}))[0], 409)
            status, source = await post("/v1/audio/source", headers=headers, json=payload)
            self.assertEqual(status, 200)
            sid = source["source_id"]
            cmd = {"op": "start", "request_id": "music", "zone_id": "zone", "devices": ["bass", "treble"],
                   "effect": {"kind": "audio_spectrum", "audio_source": sid}}
            self.assertEqual((await post("/v1/preview", headers=headers,
                                        json={**cmd, "effect": {**cmd["effect"], "audio_source": "missing"}}))[0], 400)
            self.assertEqual((await post("/v1/preview", headers=headers, json=cmd))[0], 200)
            frame_headers = {**headers, "Content-Type": "application/octet-stream"}
            async def frame(seq, data, instance=runtime.instance_id):
                return await post("/v1/audio/frame/" + sid, headers=frame_headers,
                                  params={"instance_id": instance, "sequence": str(seq)}, data=data)
            self.assertEqual((await frame(0, tone(94), "old"))[0], 409)
            self.assertEqual((await frame(0, b"bad"))[0], 400)
            self.assertEqual((await frame(0, tone(94)))[0], 200)
            self.assertEqual((await frame(0, tone(6000)))[0], 400)
            runtime.tick()
            self.assertGreater(runtime.targets["bass"]["target"]["brightness"], .25)
            self.assertLess(runtime.targets["treble"]["target"]["brightness"], .03)
            now[0] = .05
            self.assertEqual((await frame(1, tone(6000)))[0], 200)
            runtime.tick()
            self.assertGreater(runtime.targets["treble"]["target"]["brightness"], .25)
            self.assertLess(runtime.targets["bass"]["target"]["brightness"], .03)
            now[0] = 2.
            runtime.tick()
            self.assertEqual(runtime.state()["audio_sources"][0]["status"], "lost")
            self.assertAlmostEqual(runtime.targets["bass"]["target"]["brightness"], .06)
            self.assertEqual((await post("/v1/audio/source", headers=headers,
                         json={"op":"close", "instance_id":runtime.instance_id, "source_id":sid}))[0], 200)
            self.assertEqual((await frame(2, SILENCE))[0], 400)
            self.assertEqual(runtime.state()["audio_sources"], [])
