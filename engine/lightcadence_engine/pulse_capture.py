"""Explicit same-host PulseAudio/PipeWire capture via the installed parec utility.

No audio files, device discovery, remote engine connection or shell execution.
The caller selects an already authorized local source; no default source.
"""
import argparse
import asyncio
from pathlib import Path
from time import monotonic

from aiohttp import ClientSession, ClientTimeout

from .audio import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE, FRESH_SECONDS


def capture_argv(device, kind):
    if not isinstance(device, str) or not device.strip() or "\x00" in device or device.startswith("@"):
        raise ValueError("select a concrete local PulseAudio source")
    if kind not in ("pulse_monitor", "pulse_microphone"):
        raise ValueError("select capture kind")
    # Typical sink monitors have a .monitor suffix. A microphone must not
    # silently select a monitor, nor may a monitor request select a microphone.
    if device.endswith(".monitor") != (kind == "pulse_monitor"):
        raise ValueError("source name does not match explicit capture kind")
    return ["parec", "--raw", "--format=s16le", f"--rate={SAMPLE_RATE}",
            "--channels=1", "--latency-msec=20", "--device=" + device]


async def forward_latest(reader, send, clock=monotonic):
    """One latest raw frame, separate reader from HTTP; drop rather than backlog.

    Timestamp is local pipe-read completion, NOT a device capture timestamp.
    Sequence counts every complete frame, including dropped frames.
    """
    ready = asyncio.Event()
    latest = None
    ended = False

    async def drain():
        nonlocal latest, ended
        sequence = 0
        try:
            while True:
                try:
                    frame = await reader.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError:
                    return
                latest = (sequence, clock(), frame)
                sequence += 1
                ready.set()
        finally:
            ended = True
            ready.set()

    producer = asyncio.create_task(drain())
    try:
        while True:
            await ready.wait()
            ready.clear()
            item, latest = latest, None
            if item is not None and clock() - item[1] <= FRESH_SECONDS:
                await send(*item)
            if ended and latest is None:
                await producer
                break
    finally:
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass


async def capture(port, token, name, device, kind):
    if not 1 <= port <= 65535:
        raise ValueError("invalid loopback port")
    argv = capture_argv(device, kind)
    base = f"http://127.0.0.1:{port}/v1"
    async with ClientSession(headers={"Authorization": "Bearer " + token},
                             timeout=ClientTimeout(total=5)) as client:
        async def request(method, path, **kwargs):
            async with client.request(method, base + path, allow_redirects=False, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        instance = (await request("GET", "/state"))["instance_id"]
        info = await request("POST", "/audio/source",
                             json={"op": "open", "instance_id": instance, "name": name,
                                   "kind": kind, "clock_mapping": "local_read_monotonic"})
        source = info["source_id"]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL,
                                                        stdin=asyncio.subprocess.DEVNULL,
                                                        limit=FRAME_BYTES * 2)
            async def send(sequence, timestamp, frame):
                await request("POST", "/audio/frame/" + source,
                              params={"instance_id": instance, "sequence": str(sequence),
                                      "read_time": repr(timestamp)}, data=frame,
                              headers={"Content-Type": "application/octet-stream"})
            await forward_latest(proc.stdout, send)
            if await proc.wait() != 0:
                raise RuntimeError("local audio capture ended with an error")
        finally:
            try:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    # Drain discarded pipe data so a full pipe cannot block wait.
                    try:
                        await asyncio.wait_for(proc.communicate(), timeout=2)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.communicate()
            finally:
                await request("POST", "/audio/source",
                              json={"op": "close", "instance_id": instance, "source_id": source})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17842)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--kind", required=True, choices=("pulse_monitor", "pulse_microphone"))
    args = parser.parse_args()
    asyncio.run(capture(args.port, args.token_file.read_text(encoding="ascii").strip(),
                        args.name, args.device, args.kind))


if __name__ == "__main__":
    main()
