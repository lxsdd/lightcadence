"""Explicit Tuya 3.5 HSV light candidate via pinned TinyTuya, not a scanner.

The owner-local profile declares the three verified datapoints. No guessed
DPS discovery, cloud, automatic reconnect, fallback writes or cached readback.
"""
import asyncio
import colorsys
from dataclasses import dataclass, field
from math import isfinite
import logging
import re

from .chroma import ChromaV2Adapter, _srgb
from .effects import LightTarget
from .enrollment import ChromaProfile, Observation
from .runtime import PhysicalRuntime
from .sessions import DeviceState
from .yeelight_lan import YeelightLanStatePort, YeelightTcpOutput


class TuyaError(OSError):
    pass


@dataclass(frozen=True, slots=True)
class TuyaBinding:
    profile: ChromaProfile
    lan_id: str
    local_key: str = field(repr=False)
    power_dp: int
    mode_dp: int
    colour_dp: int

    def __post_init__(self):
        if (not isinstance(self.profile, ChromaProfile)
                or self.profile.transport != "tuya_v35"
                or not isinstance(self.lan_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", self.lan_id)
                or not isinstance(self.local_key, str)
                or len(self.local_key.encode("latin1")) != 16
                or not all(type(n) is int and 1 <= n <= 255 for n in self.dps)
                or len(set(self.dps)) != 3):
            raise ValueError("invalid explicit Tuya RGB binding")

    @property
    def dps(self):
        return self.power_dp, self.mode_dp, self.colour_dp


def _linear(v):
    return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4


def decode_state(power, hsv):
    if type(power) is not bool or not isinstance(hsv, str):
        raise ValueError("invalid Tuya HSV state")
    hsv = hsv.lower()
    if not re.fullmatch(r"[0-9a-f]{12}", hsv):
        raise ValueError("unsupported Tuya colour encoding")
    h, s, v = (int(hsv[i:i+4], 16) for i in (0, 4, 8))
    if h > 360 or s > 1000 or v > 1000:
        raise ValueError("invalid Tuya HSV channels")
    rgb = colorsys.hsv_to_rgb((h % 360) / 360, s / 1000, 1.)
    return DeviceState(power, LightTarget(*(_linear(c) for c in rgb), v / 1000),
                       native_hsv=hsv)


def project_state(state):
    if not isinstance(state, DeviceState) or state.kelvin is not None:
        raise ValueError("Tuya RGB target required")
    if state.native_hsv is not None:
        if decode_state(state.power, state.native_hsv) != state:
            raise ValueError("inconsistent native HSV snapshot")
        return state
    target = state.target
    # Preserve relative hue/saturation, project linear intensity to device V.
    rgb = tuple(_srgb(c) for c in (target.red, target.green, target.blue))
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    hsv = "%04x%04x%04x" % (round(h * 360) % 360, round(s * 1000),
                            round(v * target.brightness * 1000))
    return decode_state(state.power, hsv)


def _decode_response(binding, response):
    if (not isinstance(response, dict) or "Err" in response or "Error" in response
            or not isinstance(response.get("dps"), dict)):
        raise TuyaError("Tuya status unavailable")
    dps = response["dps"]
    try:
        if dps[str(binding.mode_dp)] != "colour":
            raise ValueError
        return decode_state(dps[str(binding.power_dp)], dps[str(binding.colour_dp)])
    except (KeyError, ValueError, TypeError):
        raise TuyaError("unsupported or incomplete Tuya RGB state") from None


def _commands(binding, state):
    return {str(binding.power_dp): state.power, str(binding.mode_dp): "colour",
            str(binding.colour_dp): state.native_hsv}


def _factory(binding, timeout):
    # Do not instantiate a discovery-capable BulbDevice. No auto-IP or cloud.
    import tinytuya
    logging.getLogger("tinytuya").setLevel(logging.CRITICAL + 1)
    device = tinytuya.Device(binding.lan_id, binding.profile.host, binding.local_key,
                             version=3.5, persist=True, port=binding.profile.port,
                             connection_timeout=timeout, connection_retry_limit=1,
                             connection_retry_delay=0, send_retry_limit=0)
    device.set_retry(False)
    return device


async def _settled_call(func):
    # Do not relinquish a lease or close a transport while a blocking library
    # call is still running, even if the awaiting coroutine was cancelled.
    task = asyncio.create_task(asyncio.to_thread(func))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


class TuyaStatePort:
    def __init__(self, bindings, *, timeout=2., factory=_factory):
        bindings = tuple(bindings)
        if (not bindings or not all(isinstance(b, TuyaBinding) for b in bindings)
                or type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid Tuya authority")
        for key in (lambda b: b.profile.device_id, lambda b: b.profile.ha_registry_id,
                    lambda b: b.lan_id, lambda b: (b.profile.host, b.profile.port)):
            if len({key(b) for b in bindings}) != len(bindings):
                raise ValueError("duplicate Tuya binding")
        self.bindings = {b.profile.device_id: b for b in bindings}
        self.locks = {d: asyncio.Lock() for d in self.bindings}
        self.timeout, self.factory = timeout, factory

    def binding(self, profile):
        b = self.bindings.get(profile.device_id)
        if b is None or b.profile != profile:
            raise TuyaError("Tuya profile not explicitly bound")
        return b

    def normalize(self, profile, state):
        self.binding(profile)
        return project_state(state)

    def open_device(self, binding):
        try:
            return self.factory(binding, self.timeout)
        except Exception:
            raise TuyaError("Tuya transport unavailable") from None

    def status(self, device, binding):
        state = _decode_response(binding, device.status())
        # One initial connection attempt is needed to open the socket.
        # Thereafter forbid reconnect even if the persistent socket drops.
        device.set_socketRetryLimit(0)
        return state

    @staticmethod
    def submit(device, values):
        reply = device.set_multiple_values(values, nowait=False)
        # Empty ACK can be valid, but only with a successful correlated
        # protocol return code. ACK is never independent state confirmation.
        if (device.cmd_retcode != 0 or
                (reply is not None and
                 (not isinstance(reply, dict) or "Err" in reply or "Error" in reply))):
            raise TuyaError("Tuya write uncertain or rejected")

    async def read(self, profile):
        b = self.binding(profile)
        async with self.locks[profile.device_id]:
            def transaction():
                device = self.open_device(b)
                try:
                    return self.status(device, b)
                finally:
                    device.close()
            try:
                state = await _settled_call(transaction)
            except Exception:
                raise TuyaError("Tuya status unavailable or unsupported") from None
        # Model/FW are owner-enrolled metadata, not reported by this status API.
        return Observation(profile.device_id, profile.ha_registry_id, profile.model,
                           profile.firmware, "rgb", state)

    async def write(self, profile, state):
        b = self.binding(profile)
        state = self.normalize(profile, state)
        async with self.locks[profile.device_id]:
            def transaction():
                device = self.open_device(b)
                try:
                    current = self.status(device, b)
                    if current == state:
                        return True
                    if not current.power and not state.power:
                        # Never flash an off device to alter hidden attributes.
                        return False
                    self.submit(device, _commands(b, state))
                    return True
                finally:
                    device.close()
            try:
                return await _settled_call(transaction)
            except Exception:
                raise TuyaError("Tuya write uncertain or rejected") from None


class TuyaOutput:
    def __init__(self, profile, authority):
        self.profile, self.authority = profile, authority
        self.device = None
        self.last = None

    def close(self):
        device, self.device = self.device, None
        self.last = None
        if device is not None:
            device.close()

    async def acquire(self):
        if self.device is not None:
            raise TuyaError("Tuya transport already acquired")
        b = self.authority.binding(self.profile)
        async with self.authority.locks[self.profile.device_id]:
            self.device = self.authority.open_device(b)
            try:
                await _settled_call(lambda: self.authority.status(self.device, b))
            except BaseException:
                self.close()
                raise

    async def maintain(self):
        if self.device is None:
            raise TuyaError("Tuya transport unavailable; explicit stop required")

    async def send_frame(self, frame):
        await self.maintain()
        if frame.device_id != self.profile.device_id:
            raise TuyaError("frame does not match enrolled device")
        from .effects import KelvinTarget
        if isinstance(frame.target, KelvinTarget):
            raise TuyaError("native Kelvin output unsupported")
        b = self.authority.binding(self.profile)
        state = project_state(DeviceState(True, frame.target))
        values = (_commands(b, state) if int(state.native_hsv[8:], 16)
                  else {str(b.power_dp): False})
        if values == self.last:
            return
        try:
            await _settled_call(lambda: self.authority.submit(self.device, values))
        except BaseException:
            self.close()
            raise
        self.last = values


class MixedStatePort:
    def __init__(self, yeelight, tuya):
        self.yeelight, self.tuya = yeelight, tuya

    def port(self, profile):
        return self.tuya if profile.transport == "tuya_v35" else self.yeelight

    def normalize(self, profile, state):
        return self.tuya.normalize(profile, state)

    async def read(self, profile):
        return await self.port(profile).read(profile)

    async def write(self, profile, state):
        return await self.port(profile).write(profile, state)


def compose_mixed_candidate(yeelight, tuya, *, enabled=False, journal=None, timeout=2., factory=_factory):
    if enabled is not True:
        raise ValueError("physical candidate requires explicit opt-in")
    yeelight, tuya = tuple(yeelight), tuple(tuya)
    profiles = tuple(b.profile for b in (*yeelight, *tuya))
    # A device endpoint may not be enrolled twice through different protocols.
    if len({p.host for p in profiles}) != len(profiles):
        raise ValueError("duplicate physical host across transport profiles")
    y_port = YeelightLanStatePort(yeelight, timeout=timeout) if yeelight else None
    t_port = TuyaStatePort(tuya, timeout=timeout, factory=factory)
    def transport(profile, clock):
        if profile.transport == "tuya_v35":
            return TuyaOutput(profile, t_port)
        if profile.transport == "yeelight_tcp":
            return YeelightTcpOutput(profile, y_port, clock)
        return ChromaV2Adapter(profile.device_id, profile.host, profile.port, clock=clock)
    return PhysicalRuntime(profiles, MixedStatePort(y_port, t_port), enabled=True, journal=journal,
                           transport_factory=transport)
