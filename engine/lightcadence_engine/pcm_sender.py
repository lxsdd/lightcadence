"""Owner-started loopback PCM adapter. Reads stdin; never opens capture devices."""
import argparse
import asyncio
import sys
from pathlib import Path
from time import monotonic

from aiohttp import ClientSession, ClientTimeout

from .audio import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE


async def send_pcm(port, token, name):
    if not 1 <= port <= 65535:
        raise ValueError("invalid loopback port")
    base = f"http://127.0.0.1:{port}/v1"
    async with ClientSession(headers={"Authorization": "Bearer " + token},
                             timeout=ClientTimeout(total=5)) as client:
        async def request(method, path, **kwargs):
            async with client.request(method, base + path, allow_redirects=False, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        state = await request("GET", "/state")
        instance = state["instance_id"]
        source = (await request("POST", "/audio/source",
                                json={"op": "open", "instance_id": instance, "name": name}))["source_id"]
        try:
            sequence = 0
            deadline = monotonic()
            while True:
                frame = await asyncio.to_thread(sys.stdin.buffer.read, FRAME_BYTES)
                if len(frame) != FRAME_BYTES:
                    break
                await asyncio.sleep(max(0., deadline - monotonic()))
                await request("POST", "/audio/frame/" + source,
                              params={"instance_id": instance, "sequence": str(sequence)},
                              data=frame, headers={"Content-Type": "application/octet-stream"})
                sequence += 1
                # Never send a burst to catch up after a stall.
                deadline = max(deadline + FRAME_SAMPLES / SAMPLE_RATE, monotonic())
        finally:
            await request("POST", "/audio/source",
                          json={"op": "close", "instance_id": instance, "source_id": source})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17842)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    asyncio.run(send_pcm(args.port, args.token_file.read_text(encoding="ascii").strip(), args.name))


if __name__ == "__main__":
    main()
