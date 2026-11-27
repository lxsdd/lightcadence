"""One render loop and explicit, mutually exclusive preview/physical runtimes."""
import asyncio
from dataclasses import asdict
from time import monotonic
from uuid import uuid4

from .effects import LightTarget, linear_channel, decode_effect, Effect, EffectKind
from .calibration import compile_profiles, raw_targets, selector
from .sessions import DeviceState, Phase, SessionManager, StopPolicy
from .enrollment import PhysicalSessions
from .audio import AudioHub
from .library import ProjectLibrary
from .scene_launch import SceneLaunches


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
    if isinstance(body, dict) and "kelvin" in body:
        fields(body, ("power", "kelvin", "brightness_pct"))
        bright = body["brightness_pct"]
        if type(bright) is not int or not 1 <= bright <= 100 or type(body["kelvin"]) is not int:
            raise ValueError("invalid CT state")
        return DeviceState(body["power"], LightTarget(1, 1, 1, bright / 100), body["kelvin"])
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
        self.scene_launches = SceneLaunches()
        self.audio = AudioHub(clock)
        self.manager.audio_provider = self.audio.levels
        self.manager.target_correction = self.calibrate
        self._calibration_cache = (None, {})
        self.device_bindings = {}
        self.targets = {}

    def calibrate(self, device, target):
        # Cache compiled immutable profiles by library revision. The normal
        # manager calls this once after all scene/mixer/audio composition.
        lib = self.library
        key = (id(lib), lib.data["revision"])
        if self._calibration_cache[0] != key:
            self._calibration_cache = (key, compile_profiles(lib.data["calibrations"]))
        binding = self.device_bindings.get(device)
        if binding is None or getattr(self, "device_transports", {}).get(device, {}).get("white_kelvin") is not None:
            return target
        profiles = self._calibration_cache[1]
        correction = profiles.get(selector({**binding, "device_id": device})) or profiles.get(selector(binding))
        return target if correction is None else correction.apply(target)

    def library_command(self, body):
        if body.get("kind") == "calibrations":
            # No ordinary running show may change calibration under its feet.
            # Raw comparisons are explicitly exempt because they bypass correction.
            if any(s.phase is not Phase.STOPPED and self.manager.comparison_state(s.id) is None
                   for s in self.manager.snapshots()) or getattr(getattr(self, "physical", None), "_reserved", set()):
                raise ValueError("stop ordinary shows before changing calibration")
            fields(body, ("op", "revision", "kind", "id", "value"))
            if body["op"] == "library_save":
                if not isinstance(body["value"], dict):
                    raise ValueError("invalid calibration value")
                sel = body["value"].get("selector")
                selector(sel)
                device = sel.get("device_id")
                if device is not None and self.device_bindings.get(device) != {k:v for k,v in sel.items() if k != "device_id"}:
                    raise ValueError("calibration binding mismatch")
                if device is None and sel not in self.device_bindings.values():
                    raise ValueError("unknown enrolled model binding")
        self.library.command(body)

    def tick(self):
        at = self.clock()
        active = {}
        for session in self.manager.snapshots():
            if session.phase is not Phase.RUNNING:
                continue
            if self.manager.auto_end_due(session.id, at):
                self.manager.begin_stop(session.id, session.revision)
                self.manager.finish_stop(session.id, self.manager.snapshot(session.id).revision)
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
        origin = self.scene_launches.origins.get(session.id)
        if origin is not None:
            result["scene_origin"] = dict(origin)
        comparison = self.manager.comparison_state(session.id)
        if comparison is not None:
            result["comparison"] = {"reference": comparison["reference"], "targets": {d:asdict(t) for d,t in comparison["targets"].items()}}
        elif session.phase is Phase.RUNNING:
            result["mix"] = self.manager.mix_state(session.id)
        timeline = self.manager.timeline_state(session.id)
        if timeline is not None:
            if session.effect.auto_end:
                timeline["auto_end"] = True
                timeline["ending"] = session.id in getattr(self, "_ending", {})
            result["timeline"] = timeline
        return result

    def command(self, body):
        if isinstance(body, dict) and body.get("op") == "scene_start":
            replay, launch = self.scene_launches.prepare(body, self.library)
            if replay is None:
                request, zone, devices, effect, _policy, positions, origin = launch
                self.validate_audio(effect)
                self.scene_launches.begin(body)
                session = self.manager.start(request, zone,
                    {d: DeviceState(False, LightTarget(0, 0, 0, 0)) for d in devices},
                    effect, StopPolicy.HOLD, positions)
                self.scene_launches.finish(body, session, origin)
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "mix_control":
            fields(body, ("op", "session_id", "revision", "action", "value"), ("index",))
            if (body["action"] != "master") != ("index" in body):
                raise ValueError("group index required only for group controls")
            self.manager.mix_control(identity(body["session_id"]), body["revision"],
                                     body["action"], body["value"], body.get("index"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "timeline_control":
            fields(body, ("op", "session_id", "revision", "action"), ("position",))
            if (body["action"] == "seek") != ("position" in body):
                raise ValueError("position required only for seek")
            self.manager.timeline_control(identity(body["session_id"]), body["revision"],
                                          body["action"], body.get("position"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") in ("library_save", "library_delete", "library_import"):
            self.library_command(body)
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

    def __init__(self, profiles, state_port, *, enabled=False, clock=monotonic, journal=None,
                 transport_factory=None):
        if enabled is not True:
            raise ValueError("physical candidate requires explicit opt-in")
        self.clock = clock
        self.instance_id = str(uuid4())
        profiles = tuple(profiles)
        self.physical = PhysicalSessions(profiles, state_port, enabled=True, clock=clock, journal=journal,
                                         transport_factory=transport_factory)
        self.manager = self.physical.manager
        self.library = ProjectLibrary()
        self.scene_launches = SceneLaunches()
        self.audio = AudioHub(clock)
        self.manager.audio_provider = self.audio.levels
        self.manager.target_correction = self.calibrate
        self._calibration_cache = (None, {})
        self.device_bindings = {p.device_id: {"model":p.model,"firmware":p.firmware,"transport":p.transport} for p in profiles}
        self.targets = {}
        self._closing = False
        self._manual = {}
        self._ending = {}
        self.enrolled_devices = tuple(sorted(p.device_id for p in profiles))
        self.device_transports = {p.device_id: {"kind": p.transport, "interval_seconds": p.interval,
            **({"white_kelvin": p.white_kelvin, "ct_min_kelvin": p.ct_min_kelvin, "ct_max_kelvin": p.ct_max_kelvin}
               if p.white_kelvin is not None else {})} for p in profiles}

    def tick(self):
        if self._closing:
            return
        at = self.clock()
        for session in self.manager.snapshots():
            if self.manager.auto_end_due(session.id, at):
                # Fence output synchronously, before scheduling one stop transaction.
                self.manager.begin_stop(session.id, session.revision)
                revision = self.manager.snapshot(session.id).revision
                task = asyncio.create_task(self.physical.stop(session.id, revision))
                self._ending[session.id] = task
                task.add_done_callback(lambda done, sid=session.id: self._end_done(sid, done))
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

    def _end_done(self, sid, task):
        self._ending.pop(sid, None)
        if not task.cancelled():
            task.exception()
        # Failed or uncertain stop remains STOPPING; only explicit retry can
        # repeat it. No auto retry, no release of unconfirmed device leases.

    def state(self):
        result = super().state()
        result.update(output_mode="physical_candidate", physical_output_enabled=True,
                      hardware_qualified=False, enrolled_devices=self.enrolled_devices,
                      device_transports=self.device_transports, calibration_bindings=self.device_bindings,
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
        if isinstance(body, dict) and body.get("op") == "calibration_project":
            fields(body, ("op", "revision", "devices", "targets"))
            if type(body["revision"]) is not int or body["revision"] != self.library.data["revision"]:
                from .sessions import StaleRevision
                raise StaleRevision("refresh calibration library")
            if self.library.fault:
                raise OSError("library unavailable")
            devices = body["devices"]
            if not isinstance(devices, list) or not devices or not all(isinstance(d,str) and d in self.device_bindings for d in devices) or len(set(devices)) != len(devices):
                raise ValueError("select unique enrolled lights")
            from .chroma import scene_parameters
            from .tuya_lan import project_state
            requested = raw_targets(body["targets"], devices)
            projected = {}
            for d, nominal in requested.items():
                if self.device_transports[d].get("white_kelvin") is not None or nominal.brightness == 0 or max(nominal.red,nominal.green,nominal.blue) == 0:
                    raise ValueError("initial native export requires nonblack RGB-on targets")
                target = self.calibrate(d, nominal)
                if self.device_bindings[d]["transport"] == "tuya_v35":
                    native = project_state(DeviceState(True, target)).native_hsv
                    h,s,v = (int(native[i:i+4],16) for i in (0,4,8))
                    if not v:
                        raise ValueError("calibrated target quantizes to zero")
                    attrs = {"state":"on","hs_color":[h,s/10],"brightness":max(1,round(v*255/1000))}
                    command = {"native_hsv":native}
                else:
                    _, value, bright, *_ = scene_parameters(target)
                    if not value or not round(target.brightness*100):
                        raise ValueError("calibrated target quantizes to zero")
                    attrs = {"state":"on","rgb_color":[value>>16,value>>8&255,value&255],"brightness":max(1,round(bright*255/100))}
                    command = {"rgb":attrs["rgb_color"],"brightness_pct":bright}
                projected[d] = {"nominal":asdict(nominal),"corrected":asdict(target),"native":command,"ha":attrs}
            return {**self.state(),"calibration_projection":{"instance_id":self.instance_id,"revision":body["revision"],"targets":projected}}
        if isinstance(body, dict) and body.get("op") == "calibration_start":
            fields(body, ("op", "request_id", "devices", "reference", "targets"))
            devices = body["devices"]
            if not isinstance(devices, list) or len(devices) < 2 or not all(isinstance(d, str) and d in self.device_bindings for d in devices) or len(set(devices)) != len(devices) or body["reference"] not in devices:
                raise ValueError("select distinct enrolled comparison lamps")
            if any(self.device_transports[d].get("white_kelvin") is not None for d in devices):
                raise ValueError("initial RGB comparison requires RGB capable transport")
            targets = raw_targets(body["targets"], devices)
            await self.physical.start(identity(body["request_id"]), "Calibration comparison", devices,
                Effect(EffectKind.STATIC), StopPolicy.RESTORE, comparison=targets, reference=body["reference"])
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "calibration_adjust":
            fields(body, ("op", "session_id", "revision", "targets"))
            sid = identity(body["session_id"])
            session = self.manager.snapshot(sid)
            self.manager.comparison_adjust(sid, body["revision"], raw_targets(body["targets"], session.devices))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "scene_start":
            replay, launch = self.scene_launches.prepare(body, self.library)
            if replay is None:
                request, zone, devices, effect, policy, positions, origin = launch
                self.validate_audio(effect)
                self.scene_launches.begin(body)
                session = await self.physical.start(request, zone, devices, effect, policy, positions)
                self.scene_launches.finish(body, session, origin)
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "mix_control":
            fields(body, ("op", "session_id", "revision", "action", "value"), ("index",))
            if (body["action"] != "master") != ("index" in body):
                raise ValueError("group index required only for group controls")
            self.manager.mix_control(identity(body["session_id"]), body["revision"],
                                     body["action"], body["value"], body.get("index"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") == "timeline_control":
            fields(body, ("op", "session_id", "revision", "action"), ("position",))
            if (body["action"] == "seek") != ("position" in body):
                raise ValueError("position required only for seek")
            self.manager.timeline_control(identity(body["session_id"]), body["revision"],
                                          body["action"], body.get("position"))
            self.tick()
            return self.state()
        if isinstance(body, dict) and body.get("op") in ("library_save", "library_delete", "library_import"):
            self.library_command(body)
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
            sid = identity(body["session_id"])
            if sid in self._ending:
                raise ValueError("automatic stop in progress")
            await self.physical.stop(sid, body["revision"])
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
            if self._ending:
                await asyncio.gather(*tuple(self._ending.values()), return_exceptions=True)
            await self.physical.close()
        finally:
            self.library.close()
