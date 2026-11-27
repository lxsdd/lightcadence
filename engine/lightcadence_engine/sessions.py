"""Server-owned session/lease state and latest-value render mailboxes.

All methods are synchronous and must be invoked on the owning engine event
loop. A future transport adapter must serialize per-device output and
revalidate intents immediately before I/O. This core cannot lock external
LAN clients, observe physical light output, or restore actual hardware.
"""
from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite
import re
from time import monotonic
from uuid import uuid4
from typing import Callable, Mapping

from .effects import Effect, EffectKind, LightTarget


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
    kelvin: int | None = None
    native_hsv: str | None = None

    def __post_init__(self):
        if type(self.power) is not bool or not isinstance(self.target, LightTarget):
            raise ValueError("invalid device snapshot")
        if self.kelvin is not None and (type(self.kelvin) is not int or not 1700 <= self.kelvin <= 6500
                or (self.target.red, self.target.green, self.target.blue) != (1, 1, 1)
                or not 0.01 <= self.target.brightness <= 1):
            raise ValueError("invalid color temperature snapshot")
        if self.native_hsv is not None:
            if (self.kelvin is not None or not isinstance(self.native_hsv, str)
                    or not re.fullmatch(r"[0-9a-f]{12}", self.native_hsv)):
                raise ValueError("invalid native HSV snapshot")
            h, sat, value = (int(self.native_hsv[i:i+4], 16) for i in (0, 4, 8))
            if h > 360 or sat > 1000 or value > 1000:
                raise ValueError("invalid native HSV channels")


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
    timeline_anchor: float | None = None
    timeline_offset: float = 0.
    timeline_paused: bool = False
    frozen_audio: tuple[float, float, float] | None = None
    master_gain: float = 1.
    gains: list[float] = field(default_factory=list)
    muted: list[bool] = field(default_factory=list)
    solo: list[bool] = field(default_factory=list)
    comparison: dict[str, LightTarget] | None = None
    reference: str | None = None

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
        self.audio_provider = None
        self.target_correction = None
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
              positions: Mapping[str, float] | None = None, *, comparison=None, reference=None) -> SessionSnapshot:
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
        effect.validate_targets(originals)
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
        if comparison is not None and (set(comparison) != set(originals) or reference not in originals or len(originals) < 2 or stop_policy is not StopPolicy.RESTORE or not all(isinstance(v, LightTarget) for v in comparison.values())):
            raise SessionError("invalid calibration comparison")
        fingerprint = (zone_id, tuple(originals.items()), effect, stop_policy,
                       tuple(sorted(coordinates.items())), None if comparison is None else tuple(sorted(comparison.items())), reference)
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
        session.comparison = None if comparison is None else dict(comparison)
        session.reference = reference
        count = len(effect.layers) + len(effect.tracks)
        session.gains = [1.] * count
        session.muted = [False] * count
        session.solo = [False] * count
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

    def _timeline_elapsed(self, session, at):
        anchor = session.started_at if session.timeline_anchor is None else session.timeline_anchor
        elapsed = session.timeline_offset + (0. if session.timeline_paused else max(0., at - anchor))
        return elapsed if session.effect.loop else min(elapsed, session.effect.duration)

    def timeline_state(self, session_id):
        session = self._get(session_id)
        if session.effect.kind is not EffectKind.TIMELINE or session.phase is Phase.STOPPED:
            return None
        duration = session.effect.duration
        elapsed = self._timeline_elapsed(session, self._clock())
        playhead = elapsed % duration if session.effect.loop else elapsed
        result = {"duration": duration, "playhead": playhead,
                  "ended": not session.effect.loop and elapsed >= duration, "loop": session.effect.loop,
                  "paused": session.timeline_paused}
        if session.effect.tempo_map is not None:
            result.update(beat=session.effect.authored_time(playhead),
                          duration_beats=session.effect.authored_duration)
        if session.effect.markers:
            result["markers"] = [{"name": m.name, "time": m.time,
                                  "seconds": session.effect.seconds(m.time)} for m in session.effect.markers]
        return result

    def auto_end_due(self, session_id, at):
        session = self._get(session_id)
        return (session.phase is Phase.RUNNING and session.effect.kind is EffectKind.TIMELINE
                and session.effect.auto_end and not session.timeline_paused
                and self._timeline_elapsed(session, at) >= session.effect.duration)

    def timeline_control(self, session_id, revision, action, position=None):
        session = self._revision(session_id, revision)
        if session.phase is not Phase.RUNNING or session.effect.kind is not EffectKind.TIMELINE:
            raise SessionError("timeline session not controllable")
        now = self._clock()
        if (not isfinite(now) or now < session.started_at
                or (session.last_target_time is not None and now < session.last_target_time)):
            raise SessionError("invalid engine clock")
        elapsed = self._timeline_elapsed(session, now)
        if action == "seek":
            if (type(position) not in (int, float) or not isfinite(position)
                    or not 0 <= position <= session.effect.duration):
                raise SessionError("seek outside timeline")
            elapsed = float(position)
        elif action == "pause":
            if position is not None or session.timeline_paused:
                raise SessionError("timeline already paused or invalid position")
            audio = (self.audio_provider(session.effect.audio_source, now)
                     if self.audio_provider is not None and session.effect.audio_source is not None else None)
            session.frozen_audio = tuple(audio) if audio is not None else None
            session.timeline_paused = True
        elif action == "resume":
            if position is not None or not session.timeline_paused:
                raise SessionError("timeline not paused or invalid position")
            session.timeline_paused = False
            session.frozen_audio = None
        else:
            raise SessionError("unknown timeline action")
        session.timeline_offset = elapsed % session.effect.duration if session.effect.loop else elapsed
        session.timeline_anchor = now
        session.revision += 1
        session.generation += 1
        # Invalidate queued old-position frames before new rendering. No new
        # owner or device writer; physical coordinator serializes in-flight I/O.
        for device in session.positions:
            self._mailboxes.pop(device, None)
            session.latest.pop(device, None)
        return session.snapshot()

    def mix_state(self, session_id):
        session = self._get(session_id)
        return {"master": session.master_gain, "groups": [
            {"gain": gain, "muted": mute, "solo": solo}
            for gain, mute, solo in zip(session.gains, session.muted, session.solo)]}

    def mix_control(self, session_id, revision, action, value, index=None):
        session = self._revision(session_id, revision)
        if session.phase is not Phase.RUNNING or session.comparison is not None:
            raise SessionError("session not controllable")
        if action in ("master", "gain"):
            if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
                raise SessionError("invalid fader value")
        elif action in ("mute", "solo"):
            if type(value) is not bool:
                raise SessionError("invalid toggle")
        else:
            raise SessionError("unknown mixer action")
        if action == "master":
            if index is not None:
                raise SessionError("master takes no group index")
            session.master_gain = float(value)
        else:
            if type(index) is not int or not 0 <= index < len(session.gains):
                raise SessionError("invalid mixer group index")
            if action == "gain":
                session.gains[index] = float(value)
            elif action == "mute":
                session.muted[index] = value
            else:
                session.solo[index] = value
        session.revision += 1
        session.generation += 1
        # Mix settings never change the original effect, timing, device
        # ownership or recovery intents. In-flight output is serialized by
        # the original coordinator; old queued generations are invalid.
        for device in session.positions:
            self._mailboxes.pop(device, None)
            session.latest.pop(device, None)
        return session.snapshot()

    def comparison_state(self, session_id):
        session = self._get(session_id)
        if session.comparison is None:
            return None
        return {"reference": session.reference, "targets": {d:t for d,t in session.comparison.items() if d in session.positions}}

    def comparison_adjust(self, session_id, revision, targets):
        session = self._revision(session_id, revision)
        if session.phase is not Phase.RUNNING or session.comparison is None or set(targets) != set(session.positions):
            raise SessionError("comparison session not adjustable")
        session.comparison = dict(targets)
        session.revision += 1
        session.generation += 1
        for device in session.positions:
            self._mailboxes.pop(device, None)
            session.latest.pop(device, None)
        return session.snapshot()

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
        timeline = session.effect.kind is EffectKind.TIMELINE
        elapsed = self._timeline_elapsed(session, at) if timeline else at - session.started_at
        audio = (session.frozen_audio if timeline and session.timeline_paused else
                 self.audio_provider(session.effect.audio_source, at)
                 if self.audio_provider is not None and session.effect.audio_source is not None else None)
        has_solo = any(session.solo)
        gains = tuple(0. if muted or (has_solo and not solo) else gain
                      for gain, muted, solo in zip(session.gains, session.muted, session.solo))
        for device, position in session.positions.items():
            if session.comparison is not None:
                target = session.comparison[device]
            else:
                target = session.effect.render(elapsed, position, audio, device, gains)
                target = replace(target, brightness=target.brightness * session.master_gain)
                if self.target_correction is not None:
                    target = self.target_correction(device, target)
            frame = Frame(session.id, session.generation, device, session.sequence, at, target)
            session.latest[device] = frame.sequence
            self._mailboxes[device] = frame
        return len(session.positions)

    def peek_latest(self, device_id: str) -> Frame | None:
        """Read render intent without consuming the output worker's mailbox."""
        return self._mailboxes.get(device_id)

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
            target = original if session.policy is StopPolicy.RESTORE else replace(original, power=False)
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
