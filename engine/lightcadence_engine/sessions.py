"""Server-owned session/lease state and latest-value render mailboxes.

All methods are synchronous and must be invoked on the owning engine event
loop. A future transport adapter must serialize per-device output and
revalidate intents immediately before I/O. This core cannot lock external
LAN clients, observe physical light output, or restore actual hardware.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from time import monotonic
from uuid import uuid4
from typing import Callable, Mapping

from .effects import Effect, LightTarget


class SessionError(ValueError):
    pass


class Conflict(SessionError):
    def __init__(self, owners: Mapping[str, str]):
        self.owners = dict(owners)
        super().__init__("device already belongs to another session")


class StaleRevision(SessionError):
    pass


class Phase(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class StopPolicy(StrEnum):
    RESTORE = "restore"
    OFF = "off"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class DeviceState:
    power: bool
    target: LightTarget

    def __post_init__(self):
        if type(self.power) is not bool or not isinstance(self.target, LightTarget):
            raise ValueError("invalid device snapshot")


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    id: str
    zone_id: str
    phase: Phase
    revision: int
    generation: int
    devices: tuple[str, ...]
    detached: tuple[str, ...]
    started_at: float
    effect: Effect
    stop_policy: StopPolicy


@dataclass(frozen=True, slots=True)
class Frame:
    session_id: str
    generation: int
    device_id: str
    sequence: int
    target_time: float
    target: LightTarget


@dataclass(frozen=True, slots=True)
class StopIntent:
    session_id: str
    generation: int
    device_id: str
    state: DeviceState


@dataclass(slots=True)
class _Session:
    id: str
    zone_id: str
    effect: Effect
    positions: dict[str, float]
    originals: dict[str, DeviceState]
    policy: StopPolicy
    started_at: float
    phase: Phase = Phase.RUNNING
    revision: int = 1
    generation: int = 1
    detached: set[str] = field(default_factory=set)
    sequence: int = 0
    latest: dict[str, int] = field(default_factory=dict)
    pending_stop: dict[str, StopIntent] = field(default_factory=dict)
    stop_failed: set[str] = field(default_factory=set)
    last_target_time: float | None = None

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            self.id, self.zone_id, self.phase, self.revision, self.generation,
            tuple(self.positions), tuple(sorted(self.detached)), self.started_at,
            self.effect, self.policy,
        )


class SessionManager:
    """One in-process lease per device, independent sessions, revisioned reads."""

    def __init__(self, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._owners: dict[str, str] = {}
        self._requests: dict[str, tuple[tuple, str]] = {}
        self._mailboxes: dict[str, Frame] = {}

    @staticmethod
    def _identity(value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise SessionError("stable non-empty identity required")

    def _get(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionError("unknown session") from exc

    def _revision(self, session_id: str, revision: int) -> _Session:
        session = self._get(session_id)
        if type(revision) is not int or revision != session.revision:
            raise StaleRevision("refresh the server snapshot")
        return session

    def snapshot(self, session_id: str) -> SessionSnapshot:
        return self._get(session_id).snapshot()

    def snapshots(self) -> tuple[SessionSnapshot, ...]:
        return tuple(s.snapshot() for s in self._sessions.values())

    def owner(self, device_id: str) -> str | None:
        return self._owners.get(device_id)

    def start(self, request_id: str, zone_id: str, devices: Mapping[str, DeviceState],
              effect: Effect, stop_policy: StopPolicy = StopPolicy.RESTORE,
              positions: Mapping[str, float] | None = None) -> SessionSnapshot:
        self._identity(request_id)
        self._identity(zone_id)
        if not isinstance(effect, Effect) or not isinstance(stop_policy, StopPolicy):
            raise SessionError("invalid effect or stop policy")
        originals = dict(devices)
        if not originals:
            raise SessionError("select at least one device")
        for device, state in originals.items():
            self._identity(device)
            if not isinstance(state, DeviceState):
                raise SessionError("invalid original device state")
        if positions is None:
            coordinates = {d: i / max(1, len(originals) - 1)
                           for i, d in enumerate(originals)}
        else:
            coordinates = dict(positions)
            if coordinates.keys() != originals.keys():
                raise SessionError("positions must match selected devices")
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                       and isfinite(x) and 0 <= x <= 1 for x in coordinates.values()):
                raise SessionError("invalid device position")
        fingerprint = (zone_id, tuple(originals.items()), effect, stop_policy,
                       tuple(sorted(coordinates.items())))
        previous = self._requests.get(request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise SessionError("request ID reused with different content")
            return self.snapshot(previous[1])
        conflicts = {d: self._owners[d] for d in originals if d in self._owners}
        if conflicts:
            raise Conflict(conflicts)
        now = self._clock()
        if not isfinite(now):
            raise SessionError("invalid engine clock")
        session = _Session(str(uuid4()), zone_id, effect, coordinates, originals,
                           stop_policy, now)
        self._sessions[session.id] = session
        self._owners.update((d, session.id) for d in originals)
        self._requests[request_id] = (fingerprint, session.id)
        return session.snapshot()

    def detach(self, session_id: str, revision: int, device_id: str) -> SessionSnapshot:
        session = self._revision(session_id, revision)
        if self._owners.get(device_id) != session.id:
            raise SessionError("device is not owned by session")
        self._release_device(session, device_id)
        session.detached.add(device_id)
        session.revision += 1
        if not session.positions:
            session.phase = Phase.STOPPED
            session.generation += 1
        return session.snapshot()

    def _release_device(self, session: _Session, device_id: str) -> None:
        del self._owners[device_id]
        del session.positions[device_id]
        del session.originals[device_id]
        session.latest.pop(device_id, None)
        session.pending_stop.pop(device_id, None)
        session.stop_failed.discard(device_id)
        self._mailboxes.pop(device_id, None)

    def render(self, session_id: str, target_time: float | None = None) -> int:
        session = self._get(session_id)
        if session.phase is not Phase.RUNNING:
            return 0
        at = self._clock() if target_time is None else target_time
        if (not isfinite(at) or at < session.started_at or
                (session.last_target_time is not None and at < session.last_target_time)):
            raise SessionError("render time must be finite and monotonic")
        session.last_target_time = at
        session.sequence += 1
        for device, position in session.positions.items():
            frame = Frame(session.id, session.generation, device, session.sequence,
                          at, session.effect.render(at - session.started_at, position))
            session.latest[device] = frame.sequence
            self._mailboxes[device] = frame
        return len(session.positions)

    def take_latest(self, device_id: str) -> Frame | None:
        return self._mailboxes.pop(device_id, None)

    def frame_is_current(self, frame: Frame) -> bool:
        session = self._sessions.get(frame.session_id)
        return bool(session and session.phase is Phase.RUNNING
                    and session.generation == frame.generation
                    and self._owners.get(frame.device_id) == session.id
                    and session.latest.get(frame.device_id) == frame.sequence)

    def begin_stop(self, session_id: str, revision: int) -> tuple[StopIntent, ...]:
        session = self._revision(session_id, revision)
        if session.phase is Phase.STOPPED:
            return ()
        if session.phase is Phase.STOPPING:
            return tuple(session.pending_stop.values())
        session.phase = Phase.STOPPING
        session.revision += 1
        session.generation += 1
        for device in tuple(session.positions):
            self._mailboxes.pop(device, None)
            session.latest.pop(device, None)
            if session.policy is StopPolicy.HOLD:
                continue
            original = session.originals[device]
            target = original if session.policy is StopPolicy.RESTORE else DeviceState(
                False, original.target)
            session.pending_stop[device] = StopIntent(
                session.id, session.generation, device, target)
        return tuple(session.pending_stop.values())

    def stop_intent_is_current(self, intent: StopIntent) -> bool:
        session = self._sessions.get(intent.session_id)
        return bool(session and session.phase is Phase.STOPPING
                    and session.generation == intent.generation
                    and self._owners.get(intent.device_id) == session.id
                    and session.pending_stop.get(intent.device_id) == intent)

    def acknowledge_stop(self, intent: StopIntent, *, success: bool) -> SessionSnapshot:
        """Only transport results may acknowledge; failed restores keep ownership.

        A failed operation can be retried using the still-current intent or
        explicitly detached by the caller. It cannot be silently treated as success.
        """
        if type(success) is not bool or not self.stop_intent_is_current(intent):
            raise SessionError("stale or invalid stop acknowledgment")
        session = self._get(intent.session_id)
        if success:
            session.pending_stop.pop(intent.device_id)
            session.stop_failed.discard(intent.device_id)
        else:
            session.stop_failed.add(intent.device_id)
        session.revision += 1
        return session.snapshot()

    def finish_stop(self, session_id: str, revision: int) -> SessionSnapshot:
        session = self._revision(session_id, revision)
        if session.phase is Phase.STOPPED:
            return session.snapshot()
        if session.phase is not Phase.STOPPING or session.pending_stop:
            raise SessionError("stop has unfinished device intents")
        for device in tuple(session.positions):
            self._release_device(session, device)
        session.phase = Phase.STOPPED
        session.revision += 1
        return session.snapshot()
