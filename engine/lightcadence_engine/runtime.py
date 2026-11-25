"""Single-loop preview runtime. No device adapters or physical output."""
import asyncio
from dataclasses import asdict
from time import monotonic
from uuid import uuid4

from .effects import Effect, EffectKind, LightTarget
from .sessions import DeviceState, Phase, SessionManager, StopPolicy


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        raise ValueError("invalid request fields")
    return value


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty identity required")
    return value


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
            devices = body["devices"]
            if not isinstance(devices, list) or not devices:
                raise ValueError("select preview devices")
            for device in devices:
                identity(device)
            if len(set(devices)) != len(devices):
                raise ValueError("duplicate preview device")
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
            self.manager.start(identity(body["request_id"]), identity(body["zone_id"]),
                               {d: DeviceState(False, LightTarget(0, 0, 0, 0)) for d in devices},
                               Effect(**params), StopPolicy.HOLD, positions)
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
