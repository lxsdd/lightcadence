"""Explicit, directed Yeelight LAN state authority for the existing Chroma runtime.

The discovery reply binds the configured identity to the configured TCP endpoint.
This is device-reported LAN identity, not cryptographic authentication. No
network scans, cache fallback, speculative retries or per-frame TCP operations.
Default deployment remains preview-only.
"""
import asyncio
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import dataclass
import json
from math import isfinite
import re
import socket

from .chroma import ChromaV2Adapter, _srgb, _unique_pairs, scene_parameters
from .effects import LightTarget, linear_channel
from .enrollment import ChromaProfile, Observation
from .runtime import PhysicalRuntime
from .sessions import DeviceState

_SEARCH = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n\r\n'
_PROPERTIES = ["power", "bright", "rgb", "color_mode", "flowing", "music_on"]
_ID = re.compile(r"0x[0-9a-fA-F]{1,16}")


class LanStateError(OSError):
    """Safe error without device-provided content, addresses or identifiers."""


@dataclass(frozen=True, slots=True)
class LanBinding:
    profile: ChromaProfile
    lan_id: str
    tcp_port: int = 55443
    discovery_port: int = 1982

    def __post_init__(self):
        if (not isinstance(self.profile, ChromaProfile)
                or not isinstance(self.lan_id, str)
                or not _ID.fullmatch(self.lan_id)):
            raise ValueError("explicit Yeelight protocol identity required")
        if self.profile.transport == "yeelight_tcp" and self.profile.port != self.tcp_port:
            raise ValueError("TCP output endpoint must match the state binding")
        for port in (self.tcp_port, self.discovery_port):
            ChromaV2Adapter(self.profile.device_id, self.profile.host, port)


def _decode(power, brightness, rgb):
    if power not in ("on", "off") or not 1 <= brightness <= 100 or not 0 <= rgb <= 0xffffff:
        raise LanStateError("invalid RGB state")
    return DeviceState(power == "on", LightTarget(
        linear_channel(rgb >> 16), linear_channel(rgb >> 8 & 255), linear_channel(rgb & 255), brightness / 100))


def _decode_ct(power, brightness, kelvin, profile):
    if (power not in ("on", "off") or not 1 <= brightness <= 100
            or not profile.ct_min_kelvin <= kelvin <= profile.ct_max_kelvin):
        raise LanStateError("invalid CT state")
    return DeviceState(power == "on", LightTarget(1, 1, 1, brightness / 100), kelvin)


def _state_parameters(state, profile):
    if not isinstance(state, DeviceState):
        raise ValueError("invalid device state")
    if state.kelvin is None:
        if profile.white_kelvin is not None:
            raise ValueError("explicit CT state required")
        rgb, bright = _encode(state)
        return ["color", rgb, bright]
    if profile.ct_min_kelvin is None:
        raise ValueError("CT not enrolled")
    bright = round(state.target.brightness * 100)
    if _decode_ct("on" if state.power else "off", bright, state.kelvin, profile) != state:
        raise ValueError("CT state not exactly representable")
    return ["ct", state.kelvin, bright]


def _encode(state):
    if not isinstance(state, DeviceState) or state.kelvin is not None:
        raise ValueError("invalid RGB state")
    t = state.target
    rgb = 0
    for channel in (t.red, t.green, t.blue):
        rgb = rgb * 256 + round(_srgb(channel) * 255)
    bright = round(t.brightness * 100)
    # Never report a quantized state as an exact original-state restoration.
    if not 1 <= bright <= 100 or _decode("on" if state.power else "off", bright, rgb) != state:
        raise ValueError("state not exactly representable by RGB LAN protocol")
    return rgb, bright


def _capabilities(raw, binding):
    if len(raw) > 8192:
        raise LanStateError("oversized discovery reply")
    try:
        lines = raw.decode("ascii").split("\r\n")
        if lines[0] != "HTTP/1.1 200 OK":
            raise ValueError
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in headers:
                raise ValueError
            headers[key] = value.strip()
        p = binding.profile
        if (headers["id"].lower() != binding.lan_id.lower()
                or headers["model"] != p.model or headers["fw_ver"] != p.firmware
                or headers["location"] != f"yeelight://{p.host}:{binding.tcp_port}"
                or not ({"get_prop", "set_scene", "set_power"} |
                        ({"set_ct_abx"} if p.ct_min_kelvin is not None else set())) <= set(headers["support"].split())):
            raise ValueError
    except (KeyError, ValueError, UnicodeError):
        raise LanStateError("discovery identity or capabilities mismatch") from None


class _Connection:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.request_id = 0

    async def call(self, method, params):
        self.request_id += 1
        data = {"id": self.request_id, "method": method, "params": params}
        self.writer.write(json.dumps(data, separators=(",", ":")).encode("ascii") + b"\r\n")
        await self.writer.drain()
        while True:
            raw = await self.reader.readline()
            if not raw.endswith(b"\r\n"):
                raise LanStateError("incomplete LAN response")
            try:
                obj = json.loads(raw, object_pairs_hook=_unique_pairs)
            except (ValueError, UnicodeError, RecursionError):
                raise LanStateError("invalid LAN response") from None
            if not isinstance(obj, dict):
                raise LanStateError("invalid LAN response")
            if obj.get("method") == "props" and "id" not in obj:
                continue  # unsolicited state is not a response or confirmation
            if (type(obj.get("id")) is not int or obj["id"] != self.request_id
                    or "error" in obj or not isinstance(obj.get("result"), list)):
                raise LanStateError("LAN request rejected or response uncorrelated")
            return obj["result"]

    async def state(self, profile):
        ct = profile.white_kelvin is not None
        properties = (["power", "bright", "rgb", "ct", "color_mode", "flowing", "music_on"]
                      if profile.dual_mode else
                      ["power", "bright", "ct", "color_mode", "flowing", "music_on"] if ct else _PROPERTIES)
        result = await self.call("get_prop", properties)
        if len(result) != len(properties) or not all(isinstance(v, str) for v in result):
            raise LanStateError("incomplete LAN state")
        if profile.dual_mode:
            power, bright, rgb, kelvin, mode, flowing, music = result
            ct = mode == "2"
            color = kelvin if ct else rgb
        else:
            power, bright, color, mode, flowing, music = result
        if mode != ("2" if ct else "1") or flowing != "0" or music != "0":
            raise LanStateError("unsupported or active external device mode")
        if not re.fullmatch(r"[0-9]{1,3}", bright) or not re.fullmatch(r"[0-9]{1,8}", color):
            raise LanStateError("invalid LAN state")
        if ct:
            return _decode_ct(power, int(bright), int(color), profile)
        return _decode(power, int(bright), int(color))

    async def apply(self, method, params):
        if await self.call(method, params) != ["ok"]:
            raise LanStateError("LAN write not acknowledged")


class YeelightLanStatePort:
    """Fresh directed discovery + low-rate TCP transactions, no HA cache.

    Readback is a separate device query through read(), not the write ACK.
    All connections are short-lived to avoid occupying HA's TCP client quota.
    A failure is propagated, never retried automatically. External LAN races
    and spoofing remain possible; hardware acceptance is separate.
    """

    def __init__(self, bindings, *, timeout=2.0):
        bindings = tuple(bindings)
        if (not bindings or any(not isinstance(b, LanBinding) for b in bindings)
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid LAN bindings or timeout")
        for key in (lambda b: b.profile.device_id, lambda b: b.profile.ha_registry_id,
                    lambda b: int(b.lan_id, 16), lambda b: (b.profile.host, b.tcp_port)):
            if len({key(b) for b in bindings}) != len(bindings):
                raise ValueError("duplicate LAN binding")
        self._bindings = {b.profile.device_id: b for b in bindings}
        self._locks = {d: asyncio.Lock() for d in self._bindings}
        self._timeout = timeout

    def _binding(self, profile):
        b = self._bindings.get(profile.device_id)
        if b is None or b.profile != profile:
            raise LanStateError("profile not explicitly bound")
        return b

    async def _probe(self, b):
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.connect((b.profile.host, b.discovery_port))
            await loop.sock_sendall(sock, _SEARCH)
            raw = await loop.sock_recv(sock, 65536)
        _capabilities(raw, b)

    @asynccontextmanager
    async def _transaction(self, profile):
        binding = self._binding(profile)
        async with self._locks[profile.device_id]:
            try:
                async with asyncio.timeout(self._timeout):
                    await self._probe(binding)
                    reader, writer = await asyncio.open_connection(
                        profile.host, binding.tcp_port, family=socket.AF_INET, limit=8192)
                    try:
                        yield _Connection(reader, writer)
                    finally:
                        writer.close()
                        await writer.wait_closed()
            except (OSError, TimeoutError, ValueError) as exc:
                if isinstance(exc, LanStateError):
                    raise
                raise LanStateError("LAN transaction unavailable or invalid") from None

    async def read(self, profile):
        async with self._transaction(profile) as conn:
            state = await conn.state(profile)
        return Observation(profile.device_id, profile.ha_registry_id, profile.model,
                           profile.firmware, "ct" if state.kelvin is not None else "rgb", state)

    async def write(self, profile, state):
        # Reject unsupported/quantized manual targets without touching the device.
        params = _state_parameters(state, profile)
        async with self._transaction(profile) as conn:
            current = await conn.state(profile)
            if current == state:
                return True
            if not state.power and not current.power:
                # Do not flash an already-off lamp to change hidden attributes.
                return False
            if state.power or current.target != state.target or current.kelvin != state.kelvin:
                await conn.apply("set_scene", params)
            if not state.power:
                await conn.apply("set_power", ["off", "sudden", 0])
            return True


class YeelightTcpOutput:
    """Explicit normal-LAN output, not music mode or a Chroma fallback.

    One persistent directed connection per acquired device. No per-frame state
    query, no automatic reconnect or retry. The normal protocol has a 60/minute
    per-connection command quota; reserve the full rolling minute before sending.
    Other clients and state transactions also count toward the device-wide quota.
    """
    def __init__(self, profile, authority, clock):
        if profile.transport != "yeelight_tcp":
            raise ValueError("normal TCP profile required")
        self.profile = profile
        self.authority = authority
        self.clock = clock
        self.connection = None
        self.attempts = deque()
        self.last = None

    def close(self):
        if self.connection is not None:
            # The output coordinator drains the in-flight request before close.
            # No future command is scheduled here.
            self.connection.writer.close()
            self.connection = None
        self.last = None

    async def acquire(self):
        if self.connection is not None:
            raise LanStateError("TCP output already acquired")
        binding = self.authority._binding(self.profile)
        async with self.authority._locks[self.profile.device_id]:
            async with asyncio.timeout(self.authority._timeout):
                await self.authority._probe(binding)
                reader, writer = await asyncio.open_connection(
                    self.profile.host, binding.tcp_port, family=socket.AF_INET, limit=8192)
                self.connection = _Connection(reader, writer)
        self.last = None
        # Keep the rolling local attempt history across acquisitions; never
        # treat reconnect as a way to bypass command quotas.

    async def maintain(self):
        if self.connection is None or self.connection.writer.is_closing():
            raise LanStateError("TCP output unavailable; explicit stop required")

    async def send_frame(self, frame):
        await self.maintain()
        if frame.device_id != self.profile.device_id:
            raise LanStateError("frame does not match enrolled device")
        if self.profile.white_kelvin is None:
            method, params = "set_scene", scene_parameters(frame.target)[:3]
        else:
            # Project linear RGB intensity onto a white-only channel. No invented RGB capability.
            t = frame.target
            level = round(100 * t.brightness * (.2126*t.red + .7152*t.green + .0722*t.blue))
            method, params = (("set_scene", ["ct", self.profile.white_kelvin, level]) if level
                              else ("set_power", ["off", "sudden", 0]))
        command = (method, params)
        if command == self.last:
            return
        now = self.clock()
        while self.attempts and now - self.attempts[0] >= 60.0:
            self.attempts.popleft()
        if len(self.attempts) >= 60:
            raise LanStateError("normal TCP local command quota reached")
        self.attempts.append(now)
        try:
            async with asyncio.timeout(self.authority._timeout):
                await self.connection.apply(method, params)
        except BaseException:
            # A failed/uncertain command is never replayed or reconnected.
            self.close()
            raise
        self.last = command


def compose_lan_candidate(bindings, *, enabled=False, timeout=2.0, journal=None):
    """Explicit composition for future owner-approved use; opens no sockets.

    No change to the installed preview CLI or HA service routing.
    """
    if enabled is not True:
        raise ValueError("physical candidate requires explicit opt-in")
    bindings = tuple(bindings)
    port = YeelightLanStatePort(bindings, timeout=timeout)
    def transport(profile, clock):
        if profile.transport == "yeelight_tcp":
            return YeelightTcpOutput(profile, port, clock)
        return ChromaV2Adapter(profile.device_id, profile.host, profile.port, clock=clock)
    return PhysicalRuntime([b.profile for b in bindings], port, enabled=True, journal=journal,
                           transport_factory=transport)
