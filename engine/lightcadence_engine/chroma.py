"""Opt-in Yeelight Chroma V2 UDP transport candidate.

Independent implementation of the wire messages described by the manufacturer's
public CUdpLight example (see docs/chroma-transport.md). Not enabled by the
preview runtime. A connected UDP socket filters datagrams by peer, not identity
or cryptographic authenticity. No discovery, implicit reconnect or queued writes.
"""
import asyncio
import ipaddress
import json
import re
import socket
from math import isfinite
from time import monotonic

from .effects import LightTarget
from .sessions import Frame, StopIntent


class ChromaError(OSError):
    """Protocol, session or transport failure; never includes token contents."""


_TOKEN = re.compile(r"[0-9a-fA-F]{32}")


def _srgb(value):
    return 12.92 * value if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055


def scene_parameters(target: LightTarget) -> list:
    """Initial RGB/brightness mapping; calibration on physical devices pending."""
    if not isinstance(target, LightTarget):
        raise ValueError("invalid light target")
    red, green, blue = (round(255 * _srgb(x)) for x in
                        (target.red, target.green, target.blue))
    brightness = round(target.brightness * 100)
    # Do not use a zero brightness argument on devices with a 1..100 range.
    # Black at minimum brightness is a candidate mapping, not a confirmed off.
    if brightness == 0:
        red = green = blue = 0
        brightness = 1
    return ["color", red << 16 | green << 8 | blue, brightness, 0, "sudden"]


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class ChromaV2Adapter:
    """Explicit acquire/renew; frame submission is synchronous and unverified.

    All calls use a single owning event loop. The caller must arrange renewal
    and an exclusive device lease. Expiry is conservative; never silently
    reacquire after a failure or transmit a frame while a handshake is pending.
    """

    def __init__(self, device_id: str, host: str, port: int = 55444, *,
                 timeout: float = 1.0, clock=monotonic):
        ip = ipaddress.IPv4Address(host)
        # Accept only explicit unicast local addresses. No DNS or broadcasts.
        allowed = (ip.is_loopback or any(ip in network for network in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"))))
        if (not allowed or ip.is_unspecified or ip.is_multicast
                or int(ip) & 255 in (0, 255)):
            raise ValueError("explicit local unicast IPv4 endpoint required")
        if (not isinstance(device_id, str) or not device_id.strip()
                or type(port) is not int or not 1 <= port <= 65535
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid device or transport settings")
        self.device_id = device_id
        self.endpoint = (str(ip), port)
        self.timeout = timeout
        self.clock = clock
        self._socket = None
        self._token = None
        self._expires = 0.0
        self._id = 0
        self._busy = False

    @property
    def ready(self):
        return (self._socket is not None and self._token is not None
                and not self._busy and self.clock() < self._expires)

    def close(self):
        self._token = None
        self._expires = 0.0
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _send(self, method, params, token=None):
        if self._socket is None:
            raise ChromaError("transport closed")
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params}
        if token is not None:
            msg["token"] = token
        packet = (json.dumps(msg, separators=(",", ":")) + "\r\n").encode("ascii")
        try:
            count = self._socket.send(packet)
        except OSError:
            self.close()
            raise ChromaError("datagram submission failed") from None
        if count != len(packet):
            self.close()
            raise ChromaError("incomplete datagram submission")
        return self._id

    async def _reply(self, request_id, validate):
        loop = asyncio.get_running_loop()
        sock = self._socket
        try:
            async with asyncio.timeout(self.timeout):
                while sock is self._socket:
                    data = await loop.sock_recv(sock, 65536)
                    if len(data) > 4096:
                        continue
                    try:
                        obj = json.loads(data, object_pairs_hook=_unique_pairs)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if (not isinstance(obj, dict) or type(obj.get("id")) is not int
                            or obj["id"] != request_id):
                        continue
                    if "error" in obj:
                        raise ChromaError("peer rejected session request")
                    result = validate(obj)
                    if result is not None:
                        return result
                    raise ChromaError("invalid correlated session response")
        except (TimeoutError, OSError):
            raise ChromaError("session response unavailable or invalid") from None
        raise ChromaError("transport closed during handshake")

    @staticmethod
    def _token_reply(obj):
        params = obj.get("params")
        token = params.get("token") if isinstance(params, dict) else None
        if (obj.get("method") == "udp_sess_token" and isinstance(token, str)
                and _TOKEN.fullmatch(token)):
            return token
        return None

    async def acquire(self):
        if self._busy or self._socket is not None:
            raise ChromaError("close existing session before acquiring")
        self._busy = True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket = sock
            sock.setblocking(False)
            sock.connect(self.endpoint)
            request_id = self._send("udp_sess_new", [])
            self._token = await self._reply(request_id, self._token_reply)
            if self._socket is not sock:
                raise ChromaError("transport changed during acquisition")
            self._expires = self.clock() + 10
        except BaseException:
            self.close()
            raise
        finally:
            self._busy = False

    async def renew(self):
        if not self.ready:
            raise ChromaError("session not ready")
        self._busy = True
        token = self._token
        sock = self._socket
        try:
            request_id = self._send("udp_sess_keep_alive", ["keeplive_interval", "10"], token)

            def validate(obj):
                params = obj.get("params")
                nested = params.get("token") if isinstance(params, dict) else None
                # The reference checks a returned token. Require the matching
                # request ID as well; the real response shape needs hardware proof.
                supplied = [v for v in (obj.get("token"), nested) if v is not None]
                return True if supplied and all(v == token for v in supplied) else None

            await self._reply(request_id, validate)
            if self._socket is not sock:
                raise ChromaError("transport changed during renewal")
            self._expires = self.clock() + 10
        except BaseException:
            self.close()
            raise
        finally:
            self._busy = False

    async def send_frame(self, frame: Frame):
        if frame.device_id != self.device_id:
            raise ChromaError("frame belongs to another device")
        if not self.ready:
            raise ChromaError("session not ready or expired")
        # No await between readiness check and submission. Never enqueue a
        # retry or automatically reacquire from a stale render frame.
        self._send("set_scene", scene_parameters(frame.target), self._token)

    async def apply_stop(self, intent: StopIntent) -> bool:
        # RGB transport submission alone cannot confirm an original device
        # snapshot or power state, especially CT/effect states. Fail closed
        # without issuing a speculative restore; coordinator retains lease.
        return False
