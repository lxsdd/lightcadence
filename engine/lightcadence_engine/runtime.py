"""One render loop and explicit, mutually exclusive preview/physical runtimes."""
import asyncio
from dataclasses import asdict
from time import monotonic
from uuid import uuid4

from .effects import LightTarget, linear_channel, decode_effect
from .sessions import DeviceState, Phase, SessionManager, StopPolicy
from .enrollment import PhysicalSessions
from .audio import AudioHub
from .library import ProjectLibrary


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        raise ValueError("invalid request fields")
    return value


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty identity required")
    return value


def parse_start(body):
    devices = body["devices"]
    if not isinstance(devices, list) or not devices:
        raise ValueError("select devices")
    for device in devices:
        identity(device)
    if len(set(devices)) != len(devices):
        raise ValueError("duplicate device")
    effect = decode_effect(body["effect"])
    positions = body.get("positions")
    if positions is not None and not isinstance(positions, dict):
        raise ValueError("invalid positions")
    return devices, effect, positions


def parse_manual_state(body):
    if isinstance(body, dict) and "rgb" in body:
        fields(body, ("power", "rgb", "brightness_pct"))
        rgb, bright = body["rgb"], body["brightness_pct"]
        if not isinstance(rgb, list) or len(rgb) != 3 or type(bright) is not int or not 1 <= bright <= 100:
            raise ValueError("invalid sRGB state")
        return DeviceState(body["power"], LightTarget(*(linear_channel(c) for c in rgb), bright / 100))
    fields(body, ("power", "target"))
    target = fields(body["target"], ("red", "green", "blue", "brightness"))
    return DeviceState(body["power"], LightTarget(**target))


class PreviewRuntime:
    """Authoritative session state and last rendered targets, independent of clients."""

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.instance_id = str(uuid4())
        self.manager = SessionManager(clock)
        self.library = ProjectLibrary()
        self.audio = AudioHub(clock)
        self.manager.audio_provider = self.audio.levels
        self.targets = {}

    def tick(self):
        at = self.clock()
        active = {}
        for session in self.manager.snapshots():
            if session.phase is not Phase.RUNNING:
                continue
            self.manager.render(session.id, at)
            for device in session.devices:
                frame = self.manager.take_latest(device)
                if frame is not None and self.manager.frame_is_current(frame):
                    active[device] = asdict(frame)
        self.targets = active

    def state(self):
        return {
            "schema_version": 1,
            "instance_id": self.instance_id,
            "output_mode": "preview",
            "physical_output_enabled": False,
            "sessions": [self.session_state(s) for s in self.manager.snapshots()],
            "targets": self.targets,
            "audio_sources": self.audio.snapshot(),
            "library": self.library.snapshot(),
        }

    def session_state(self, session):
        result = asdict(session)
        timeline = self.manager.timeline_state(session.id)
        if timeline is not None:
            result["timeline"] = timeline
        return result

    def command(self, body):
        if isinstance(body, dict) and body.get("op") == "timeline_control":
            fields(body, ("op", "session_id", "revision", "action"), ("position",))
            if (body["action"] == "seek") != ("position" in body):
                raise ValueError("position required only for seek")
            self.manager.timeline_control(identity(body["session_id"]), body["revision"],
                                          body["action"], body.get("position"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") in ("library_save", "library_delete"):
            self.library.command(body)
            return self.state()
        fields(body, ("op",), ("request_id", "zone_id", "devices", "effect", "positions",
                               "session_id", "revision", "device_id"))
        op = body["op"]
        if op == "start":
            fields(body, ("op", "request_id", "zone_id", "devices", "effect"), ("positions",))
            devices, effect, positions = parse_start(body)
            self.validate_audio(effect)
            self.manager.start(identity(body["request_id"]), identity(body["zone_id"]),
                               {d: DeviceState(False, LightTarget(0, 0, 0, 0)) for d in devices},
                               effect, StopPolicy.HOLD, positions)
        elif op == "detach":
            fields(body, ("op", "session_id", "revision", "device_id"))
            self.manager.detach(identity(body["session_id"]), body["revision"], identity(body["device_id"]))
        elif op == "stop":
            fields(body, ("op", "session_id", "revision"))
            sid = identity(body["session_id"])
            self.manager.begin_stop(sid, body["revision"])
            current = self.manager.snapshot(sid)
            self.manager.finish_stop(sid, current.revision)
        else:
            raise ValueError("unknown preview operation")
        self.tick()
        return self.state()

    def validate_audio(self, effect):
        if effect.audio_source is not None and effect.audio_source not in self.audio.sources:
            raise ValueError("unknown audio source")

    async def close(self):
        self.library.close()

    async def run(self):
        # Render cadence is a target, not a cap on physical-device performance.
        # Never enqueue missed ticks after a stall.
        interval = 1 / 60
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            self.tick()
            deadline += interval
            if deadline < loop.time():
                deadline = loop.time()
            await asyncio.sleep(max(0, deadline - loop.time()))


class PhysicalRuntime(PreviewRuntime):
    """Explicit composition candidate, default deployment remains preview-only.

    No preview/physical lease split: the only manager is the original
    PhysicalSessions manager. Targets are render intent, not measured light.
    The injected StatePort must independently verify device identity/state.
    Deployment requires explicit local configuration; hardware remains unqualified.
    """

    def __init__(self, profiles, state_port, *, enabled=False, clock=monotonic, journal=None):
        if enabled is not True:
            raise ValueError("physical candidate requires explicit opt-in")
        self.clock = clock
        self.instance_id = str(uuid4())
        profiles = tuple(profiles)
        self.physical = PhysicalSessions(profiles, state_port, enabled=True, clock=clock, journal=journal)
        self.manager = self.physical.manager
        self.library = ProjectLibrary()
        self.audio = AudioHub(clock)
        self.manager.audio_provider = self.audio.levels
        self.targets = {}
        self._closing = False
        self._manual = {}
        self.enrolled_devices = tuple(sorted(p.device_id for p in profiles))

    def tick(self):
        if self._closing:
            return
        self.physical.tick()
        active = {}
        for session in self.manager.snapshots():
            if session.phase is not Phase.RUNNING:
                continue
            for device in session.devices:
                frame = self.manager.peek_latest(device)
                if frame is not None and self.manager.frame_is_current(frame):
                    active[device] = asdict(frame)
        self.targets = active

    def state(self):
        result = super().state()
        result.update(output_mode="physical_candidate", physical_output_enabled=True,
                      hardware_qualified=False, enrolled_devices=self.enrolled_devices,
                      transport_status={
                          device: asdict(status)
                          for device, status in self.physical.output.status.items()
                          if self.manager.owner(device) == status.session_id
                          and self.manager.snapshot(status.session_id).generation == status.generation
                      })
        journal = self.physical.journal
        result.update(recovery_required=journal.snapshot() if journal is not None else [],
                      recovery_journal_fault=journal.fault if journal is not None else False)
        return result

    async def command(self, body):
        if self._closing:
            raise ValueError("runtime closing")
        if isinstance(body, dict) and body.get("op") == "timeline_control":
            fields(body, ("op", "session_id", "revision", "action"), ("position",))
            if (body["action"] == "seek") != ("position" in body):
                raise ValueError("position required only for seek")
            self.manager.timeline_control(identity(body["session_id"]), body["revision"],
                                          body["action"], body.get("position"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") in ("library_save", "library_delete"):
            self.library.command(body)
            return self.state()
        fields(body, ("op",), ("request_id", "zone_id", "devices", "effect", "positions",
                               "session_id", "revision", "device_id", "stop_policy", "state", "record_id", "decision"))
        op = body["op"]
        manual_result = None
        if op == "start":
            fields(body, ("op", "request_id", "zone_id", "devices", "effect"),
                   ("positions", "stop_policy"))
            devices, effect, positions = parse_start(body)
            self.validate_audio(effect)
            await self.physical.start(identity(body["request_id"]), identity(body["zone_id"]),
                                      devices, effect, StopPolicy(body.get("stop_policy", "restore")),
                                      positions)
        elif op == "detach":
            fields(body, ("op", "session_id", "revision", "device_id"))
            await self.physical.detach(identity(body["session_id"]), body["revision"],
                                       identity(body["device_id"]))
        elif op == "stop":
            fields(body, ("op", "session_id", "revision"))
            await self.physical.stop(identity(body["session_id"]), body["revision"])
        elif op == "recover":
            fields(body, ("op", "device_id", "record_id", "decision"))
            manual_result = await self.physical.recover(identity(body["device_id"]),
                identity(body["record_id"]), body["decision"])
        elif op in ("manual_set", "manual_off", "manual_on"):
            fields(body, ("op", "request_id", "device_id", "state") if op == "manual_set" else
                   ("op", "request_id", "device_id"))
            request_id = identity(body["request_id"])
            device = identity(body["device_id"])
            state = parse_manual_state(body["state"]) if op == "manual_set" else None
            fingerprint = (op, device, state)
            previous = self._manual.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint or previous[1] is None:
                    raise ValueError("manual request conflict or pending")
                manual_result = previous[1]
            else:
                self._manual[request_id] = (fingerprint, None)
                # Never replay an uncertain manual write under the same ID,
                # even after cancellation or loss of the HTTP response.
                try:
                    manual_result = (await self.physical.manual_set(device, state) if op == "manual_set"
                                     else await self.physical.manual_power(device, op == "manual_on"))
                except BaseException:
                    self._manual[request_id] = (fingerprint, "unconfirmed")
                    raise
                self._manual[request_id] = (fingerprint, manual_result)
        else:
            raise ValueError("unknown physical operation")
        self.tick()
        result = self.state()
        if manual_result is not None:
            result["manual_result"] = manual_result
        return result

    async def close(self):
        # The render task and HTTP handlers must have ended first. No blind
        # restore on shutdown: durable entries remain for explicit recovery.
        self._closing = True
        try:
            await self.physical.close()
        finally:
            self.library.close()
