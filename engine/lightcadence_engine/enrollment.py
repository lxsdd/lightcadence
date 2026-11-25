"""Explicit Chroma device enrollment and original-state transaction.

Not exposed by the preview runtime. The state port is an injected cooperating
state authority, not a proof of physical identity or optical output. It must
return a complete supported RGB state (including when off), and independently
read back confirmed state after a write. No discovery or implicit activation.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Protocol

from .chroma import ChromaV2Adapter
from .effects import Effect
from .output import DeviceOutput, OutputCoordinator
from .sessions import Conflict, DeviceState, SessionError, SessionManager, StopPolicy


@dataclass(frozen=True, slots=True)
class ChromaProfile:
    device_id: str
    ha_registry_id: str
    model: str
    firmware: str
    host: str
    interval: float
    port: int = 55444

    def __post_init__(self):
        if any(not isinstance(x, str) or not x.strip() for x in
               (self.device_id, self.ha_registry_id, self.model, self.firmware)):
            raise ValueError("explicit stable identities, model and firmware required")
        if (isinstance(self.interval, bool) or not isinstance(self.interval, (int, float))
                or not isfinite(self.interval) or self.interval < 0):
            raise ValueError("invalid device cadence")
        # Reuse original endpoint validation; constructing an adapter opens no socket.
        ChromaV2Adapter(self.device_id, self.host, self.port)


@dataclass(frozen=True, slots=True)
class Observation:
    device_id: str
    ha_registry_id: str
    model: str
    firmware: str
    mode: str
    state: DeviceState


class StatePort(Protocol):
    async def read(self, profile: ChromaProfile) -> Observation:
        """Return a fresh identity-bound state from the cooperating authority."""

    async def write(self, profile: ChromaProfile, state: DeviceState) -> bool:
        """Await the entire operation; True is an acknowledgment, not readback."""


class ProfiledChroma:
    def __init__(self, profile, state_port, clock):
        self.profile = profile
        self.state_port = state_port
        self.transport = ChromaV2Adapter(profile.device_id, profile.host,
                                         profile.port, clock=clock)

    async def observe(self):
        p = self.profile
        value = await self.state_port.read(p)
        if (not isinstance(value, Observation)
                or (value.device_id, value.ha_registry_id, value.model, value.firmware)
                != (p.device_id, p.ha_registry_id, p.model, p.firmware)
                or value.mode != "rgb" or not isinstance(value.state, DeviceState)):
            raise SessionError("state identity or supported complete RGB snapshot unavailable")
        return value.state

    async def send_frame(self, frame):
        await self.transport.send_frame(frame)

    async def apply_stop(self, intent):
        if intent.device_id != self.profile.device_id:
            return False
        # Never issue a restore over a still-active Chroma socket. Verify
        # identity before writing and require an independent matching readback.
        self.transport.close()
        await self.observe()
        if await self.state_port.write(self.profile, intent.state) is not True:
            return False
        return await self.observe() == intent.state

    def output_profile(self):
        return DeviceOutput(self, self.profile.interval,
                            prepare=self.transport.maintain, release=self.transport.close)


class PhysicalSessions:
    """Opt-in transaction around the existing manager and output coordinator.

    The caller owns this object on one event loop; never bypass its start,
    detach or stop paths. Reservations cover the asynchronous admission stage.
    A failed batch publishes no session and closes all acquired local sockets.
    External clients and process crashes are not covered by these reservations.
    """
    def __init__(self, profiles, state_port: StatePort, *, enabled=False, clock=monotonic):
        if enabled is not True:
            raise ValueError("physical enrollment requires explicit opt-in")
        profiles = tuple(profiles)
        if any(not isinstance(p, ChromaProfile) for p in profiles):
            raise ValueError("invalid device profile")
        for key in (lambda p: p.device_id, lambda p: p.ha_registry_id,
                    lambda p: (p.host, p.port)):
            if len({key(p) for p in profiles}) != len(profiles):
                raise ValueError("duplicate device identity, HA reference or endpoint")
        self.manager = SessionManager(clock)
        self._adapters = {p.device_id: ProfiledChroma(p, state_port, clock) for p in profiles}
        self.output = OutputCoordinator(self.manager, {
            d: a.output_profile() for d, a in self._adapters.items()}, clock)
        self._reserved = set()
        self._pending = set()
        self._requests = {}

    async def start(self, request_id, zone_id, devices, effect, policy=StopPolicy.RESTORE,
                    positions=None):
        if any(not isinstance(v, str) or not v.strip() for v in (request_id, zone_id)):
            raise SessionError("stable request and zone identity required")
        selected = tuple(devices)
        if (not selected or any(not isinstance(d, str) for d in selected)
                or len(set(selected)) != len(selected)
                or any(d not in self._adapters for d in selected)):
            raise SessionError("select unique enrolled devices")
        if not isinstance(effect, Effect) or not isinstance(policy, StopPolicy):
            raise SessionError("invalid effect or stop policy")
        coordinates = None if positions is None else dict(positions)
        if coordinates is not None and (coordinates.keys() != set(selected)
                or not all(type(v) in (int, float) and isfinite(v) and 0 <= v <= 1
                           for v in coordinates.values())):
            raise SessionError("invalid device positions")
        fingerprint = (zone_id, selected, effect, policy,
                       None if coordinates is None else tuple(sorted(coordinates.items())))
        previous = self._requests.get(request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise SessionError("request ID reused with different content")
            return self.manager.snapshot(previous[1])
        if request_id in self._pending:
            raise SessionError("request admission in progress")
        conflicts = {d: self.manager.owner(d) or "admission_pending" for d in selected
                     if self.manager.owner(d) is not None or d in self._reserved}
        if conflicts:
            raise Conflict(conflicts)
        self._reserved.update(selected)
        self._pending.add(request_id)
        attempted = []
        try:
            # Complete all snapshot reads before opening any UDP session.
            # Independent devices run concurrently; a failed child never
            # escapes the batch while its siblings are still executing.
            captured = await self._settle(
                [self._adapters[d].observe() for d in selected])
            originals = dict(zip(selected, captured, strict=True))
            attempted = [self._adapters[d].transport for d in selected]
            await self._settle([adapter.acquire() for adapter in attempted])
            session = self.manager.start(request_id, zone_id, originals, effect, policy, coordinates)
            self._requests[request_id] = (fingerprint, session.id)
            return session
        except BaseException:
            for adapter in attempted:
                adapter.close()
            raise
        finally:
            self._reserved.difference_update(selected)
            self._pending.discard(request_id)

    @staticmethod
    async def _settle(operations):
        results = await asyncio.gather(*operations, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    @asynccontextmanager
    async def manual_handover(self, device_id):
        """Reserve a device for one cooperating manual operation.

        The caller must await all its I/O inside the context, including
        cancellation cleanup. Admission/other manual writers are refused
        until exit. This is not a lock on HA or other external LAN clients.
        A stale revision or failed transport release never grants permission.
        """
        if device_id not in self._adapters:
            raise SessionError("device not enrolled")
        if device_id in self._reserved:
            raise Conflict({device_id: "operation_pending"})
        self._reserved.add(device_id)
        try:
            owner = self.manager.owner(device_id)
            if owner is not None:
                current = self.manager.snapshot(owner)
                await self.output.detach(owner, current.revision, device_id)
            if self.manager.owner(device_id) is not None:
                raise SessionError("manual operation not granted")
            yield
        finally:
            self._reserved.discard(device_id)

    async def manual_set(self, device_id, state):
        """One cooperating manual write, confirmed by independent observation."""
        if not isinstance(state, DeviceState):
            raise SessionError("invalid manual state")
        async with self.manual_handover(device_id):
            adapter = self._adapters[device_id]
            await adapter.observe()
            if await adapter.state_port.write(adapter.profile, state) is not True:
                return "unconfirmed"
            return "confirmed" if await adapter.observe() == state else "unconfirmed"

    async def close(self):
        """Called after command admission and rendering have stopped."""
        try:
            await self.output.drain()
        finally:
            for adapter in self._adapters.values():
                adapter.transport.close()

    def tick(self):
        for session in self.manager.snapshots():
            self.manager.render(session.id)
        self.output.pump()

    async def detach(self, session_id, revision, device_id):
        return await self.output.detach(session_id, revision, device_id)

    async def stop(self, session_id, revision):
        return await self.output.stop(session_id, revision)
