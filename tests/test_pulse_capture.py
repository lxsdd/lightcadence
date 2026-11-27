"""Same-host read clock and bounded capture forwarding, no real audio device."""
import asyncio
import sys
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from engine.lightcadence_engine.audio import AudioHub, FRAME_BYTES, FRESH_SECONDS
from engine.lightcadence_engine.pulse_capture import capture_argv, forward_latest
from engine.lightcadence_engine.runtime import PreviewRuntime
from engine.lightcadence_engine.server import create_app

SILENCE = bytes(FRAME_BYTES)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_source_and_clock(self):
        self.assertIn("--device=example.monitor", capture_argv("example.monitor", "pulse_monitor"))
        for name, kind in (("@DEFAULT_SOURCE@", "pulse_microphone"), ("", "pulse_microphone"),
                           ("mic", "pulse_monitor"), ("example.monitor", "pulse_microphone")):
            with self.assertRaises(ValueError):
                capture_argv(name, kind)
        now = [10.]
        hub = AudioHub(lambda: now[0])
        src = hub.open("Monitor", "pulse_monitor", "local_read_monotonic")
        hub.push(src, 0, SILENCE, 9.9)
        self.assertAlmostEqual(hub.snapshot()[0]["read_to_engine_seconds"], .1)
        for stamp in (None, float("nan"), 10.1, 9.6, 9.89):
            with self.assertRaises(ValueError):
                hub.push(src, 1, SILENCE, stamp)
        self.assertEqual(hub.snapshot()[0]["sequence"], 0)
        now[0] = 10.05
        hub.push(src, 1, SILENCE, 10.0)
        self.assertAlmostEqual(hub.snapshot()[0]["transport_jitter_seconds"], .05)
        now[0] = 10.26
        self.assertEqual(hub.snapshot()[0]["status"], "lost")
        self.assertIsNone(hub.snapshot()[0]["read_to_engine_seconds"])
        self.assertFalse(hub.snapshot()[0]["playback_clock_verified"])

    async def test_real_subprocess_pipe_drops_backlog(self):
        # Synthetic child only. Let the first send stall while the reader drains
        # later frames, then assert latest-only sequence semantics.
        code = "import sys,time; b=bytes(" + str(FRAME_BYTES) + "); sys.stdout.buffer.write(b); sys.stdout.buffer.flush(); time.sleep(.05); sys.stdout.buffer.write(b*8); sys.stdout.buffer.flush()"
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
        received = []
        async def send(seq, stamp, frame):
            received.append(seq)
            self.assertEqual(len(frame), FRAME_BYTES)
            if len(received) == 1:
                await asyncio.sleep(.12)
        try:
            await asyncio.wait_for(forward_latest(child.stdout, send), 5)
            self.assertEqual(await child.wait(), 0)
            self.assertEqual(received[-1], 8)
            self.assertLess(len(received), 9)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    async def test_authenticated_read_clock_and_legacy_wire(self):
        now = [10.]
        runtime = PreviewRuntime(lambda: now[0])
        token = "x" * 40
        headers = {"Authorization": "Bearer " + token}
        async with TestServer(create_app(token, runtime)) as server, ClientSession() as client:
            async def post(path, **kwargs):
                async with client.post(server.make_url(path), headers=headers, **kwargs) as r:
                    return r.status, await r.json()
            base = {"op": "open", "instance_id": runtime.instance_id, "name": "PCM"}
            self.assertEqual((await post("/v1/audio/source", json={**base,"kind":"pulse_monitor"}))[0], 400)
            status, obj = await post("/v1/audio/source", json={**base,"kind":"pulse_monitor","clock_mapping":"local_read_monotonic"})
            self.assertEqual(status, 200)
            sid = obj["source_id"]
            async def frame(stamp, sequence):
                params = {"instance_id":runtime.instance_id,"sequence":str(sequence)}
                if stamp is not None:
                    params["read_time"] = str(stamp)
                async with client.post(server.make_url("/v1/audio/frame/"+sid),
                    headers={**headers,"Content-Type":"application/octet-stream"},params=params,data=SILENCE) as r:
                    return r.status
            self.assertEqual(await frame(None,0),400)
            self.assertEqual(await frame(10.01,0),400)
            self.assertEqual(await frame(9.9,0),200)
            self.assertEqual(runtime.state()["audio_sources"][0]["clock_mapping"],"local_read_monotonic")
            self.assertEqual(await frame(9.89,1),400)
            self.assertEqual(await frame(10.,1),200)
            self.assertEqual((await post("/v1/audio/source",json=base))[0],200)
            self.assertEqual(runtime.state()["audio_sources"][1]["clock_mapping"],"arrival_only")
