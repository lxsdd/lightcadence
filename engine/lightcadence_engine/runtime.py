"""One render loop and explicit, mutually exclusive preview/physical runtimes."""
import asyncio
from dataclasses import asdict
from time import monotonic
from uuid import uuid4

from .effects import Effect, EffectKind, LightTarget
from .sessions import DeviceState, Phase, SessionManager, StopPolicy
from .enrollment import PhysicalSessions


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
    params = fields(body["effect"], ("kind",), ("period_seconds", "brightness", "color", "width"))
    if not isinstance(params["kind"], str):
        raise ValueError("invalid effect kind")
    params = dict(params)
    params["kind"] = EffectKind(params["kind"])
    if "color" in params:
        if not isinstance(params["color"], list):
            raise ValueError("invalid color")
        params["color"] = tuple(params["color"])
    positions = body.get("positions")
    if positions is not None and not isinstance(positions, dict):
        raise ValueError("invalid positions")
    return devices, Effect(**params), positions


def parse_manual_state(body):
    fields(body, ("power", "target"))
    target = fields(body["target"], ("red", "green", "blue", "brightness"))
    return DeviceState(body["power"], LightTarget(**target))


class PreviewRuntime:
    """Authoritative session state and last rendered targets, independent of clients."""

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.instance_id = str(uuid4())
        self.manager = SessionManager(clock)
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
            "sessions": [asdict(s) for s in self.manager.snapshots()],
            "targets": self.targets,
        }

    def command(self, body):
        fields(body, ("op",), ("request_id", "zone_id", "devices", "effect", "positions",
                               "session_id", "revision", "device_id"))
        op = body["op"]
        if op == "start":
            fields(body, ("op", "request_id", "zone_id", "devices", "effect"), ("positions",))
            devices, effect, positions = parse_start(body)
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

    async def close(self):
        """Preview owns no device resources."""

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
    """Explicit composition candidate, never selected by the installed CLI.

    No preview/physical lease split: the only manager is the original
    PhysicalSessions manager. Targets are render intent, not measured light.
    The injected StatePort must independently verify device identity/state.
    No production state authority or owner activation is provided here.
    """

    def __init__(self, profiles, state_port, *, enabled=False, clock=monotonic):
        if enabled is not True:
            raise ValueError("physical candidate requires explicit opt-in")
        self.clock = clock
        self.instance_id = str(uuid4())
        profiles = tuple(profiles)
        self.physical = PhysicalSessions(profiles, state_port, enabled=True, clock=clock)
        self.manager = self.physical.manager
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
        return result

    async def command(self, body):
        if self._closing:
            raise ValueError("runtime closing")
        fields(body, ("op",), ("request_id", "zone_id", "devices", "effect", "positions",
                               "session_id", "revision", "device_id", "stop_policy", "state"))
        op = body["op"]
        manual_result = None
        if op == "start":
            fields(body, ("op", "request_id", "zone_id", "devices", "effect"),
                   ("positions", "stop_policy"))
            devices, effect, positions = parse_start(body)
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
        elif op == "manual_set":
            fields(body, ("op", "request_id", "device_id", "state"))
            request_id = identity(body["request_id"])
            device = identity(body["device_id"])
            state = parse_manual_state(body["state"])
            fingerprint = (device, state)
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
                    manual_result = await self.physical.manual_set(device, state)
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
        # restore on shutdown: crash recovery needs a separate durable journal.
        self._closing = True
        await self.physical.close()
