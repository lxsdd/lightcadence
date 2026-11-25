"""Per-device asynchronous output coordination for the shared session engine.

Adapters are injected explicitly; this module never discovers devices or opens
network connections. The preview API does not enable it. Adapter methods must
complete all their own I/O before returning and must not enqueue future writes.
This is a cooperative in-process fence, not a lock on other LAN clients.
"""
import asyncio
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Awaitable, Callable, Protocol

from .sessions import Frame, Phase, SessionManager, SessionSnapshot, StopIntent


class Adapter(Protocol):
    async def send_frame(self, frame: Frame) -> None:
        """Return after transport submission, not after verified light output."""

    async def apply_stop(self, intent: StopIntent) -> bool:
        """Return True only for a positively confirmed stop/restore operation."""


@dataclass(frozen=True, slots=True)
class DeviceOutput:
    adapter: Adapter
    interval: float
    prepare: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self):
        if (isinstance(self.interval, bool) or
                not isinstance(self.interval, (float, int)) or
                not isfinite(self.interval) or self.interval < 0):
            raise ValueError("device cadence must be finite and nonnegative")
        if self.prepare is not None and not callable(self.prepare):
            raise ValueError("invalid output preparation hook")


@dataclass(frozen=True, slots=True)
class OutputStatus:
    session_id: str
    generation: int
    outcome: str
    sequence: int | None = None


class OutputCoordinator:
    """One in-flight write per device; newest mailbox wins while a device is busy.

    All calls belong to the manager's event loop. Once output is enabled,
    detach and stop must go through this coordinator, not directly through
    the manager. Starts may use the manager only with validated profiles and
    captured originals. No physical adapter is enabled by the preview API.
    """

    def __init__(self, manager: SessionManager, devices: dict[str, DeviceOutput],
                 clock=monotonic):
        self.manager = manager
        self.devices = dict(devices)
        self.clock = clock
        self._locks = {device: asyncio.Lock() for device in self.devices}
        self._workers: dict[str, asyncio.Task] = {}
        self._next_due: dict[str, float] = {}
        self._handover: set[str] = set()
        self.status: dict[str, OutputStatus] = {}

    def pump(self) -> None:
        """Schedule eligible outputs without waiting for any slow adapter."""
        now = self.clock()
        for device in self.devices:
            owner = self.manager.owner(device)
            if (owner is None or self.manager.snapshot(owner).phase is not Phase.RUNNING
                    or device in self._handover or device in self._workers
                    or self._locks[device].locked()
                    or now < self._next_due.get(device, float("-inf"))):
                continue
            task = asyncio.create_task(self._send(device))
            self._workers[device] = task
            task.add_done_callback(lambda done, d=device: self._worker_done(d, done))

    def _worker_done(self, device, task):
        if self._workers.get(device) is task:
            del self._workers[device]
        # Consume exceptions even if a caller does not await drain().
        if not task.cancelled():
            task.exception()

    async def _send(self, device):
        async with self._locks[device]:
            if device in self._handover:
                return
            owner = self.manager.owner(device)
            if owner is None:
                return
            before = self.manager.snapshot(owner)
            if before.phase is not Phase.RUNNING:
                return
            profile = self.devices[device]
            self._next_due[device] = self.clock() + profile.interval
            if profile.prepare is not None:
                try:
                    await profile.prepare()
                except Exception:
                    if self.manager.owner(device) == owner:
                        current = self.manager.snapshot(owner)
                        if current.phase is Phase.RUNNING and current.generation == before.generation:
                            self.status[device] = OutputStatus(
                                owner, before.generation, "transport_error")
                    return
            # Preparation may await network I/O. Revalidate ownership, phase
            # and handover before consuming the newest frame, never replay
            # a frame captured before the handshake.
            if device in self._handover or self.manager.owner(device) != owner:
                return
            current = self.manager.snapshot(owner)
            if current.phase is not Phase.RUNNING or current.generation != before.generation:
                return
            frame = self.manager.take_latest(device)
            if frame is None or not self.manager.frame_is_current(frame):
                return
            try:
                await profile.adapter.send_frame(frame)
            except Exception:
                outcome = "transport_error"
            else:
                outcome = "submitted_unverified"
            # An old completed send must not become current-session telemetry.
            if self.manager.owner(device) == frame.session_id:
                snapshot = self.manager.snapshot(frame.session_id)
                if snapshot.generation == frame.generation:
                    self.status[device] = OutputStatus(
                        frame.session_id, frame.generation, outcome, frame.sequence)

    async def drain(self) -> None:
        """Wait for currently scheduled work, without cancelling device I/O."""
        tasks = tuple(self._workers.values())
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    async def detach(self, session_id: str, revision: int, device: str) -> SessionSnapshot:
        """Drain in-flight output before releasing the device to a manual writer."""
        if device not in self.devices:
            raise ValueError("device has no configured output")
        if device in self._handover:
            raise ValueError("device handover already pending")
        self._handover.add(device)
        try:
            async with self._locks[device]:
                result = self.manager.detach(session_id, revision, device)
                self.status.pop(device, None)
                self._next_due.pop(device, None)
                return result
        finally:
            self._handover.discard(device)

    async def stop(self, session_id: str, revision: int) -> SessionSnapshot:
        """Fence render frames immediately; serialize stop writes per device.

        Missing/failed/unconfirmed adapters never acknowledge a restore.
        HOLD still drains all configured device writes before releasing leases.
        Call again with the current revision to retry unfinished stop intents.
        """
        before = self.manager.snapshot(session_id)
        intents = self.manager.begin_stop(session_id, revision)
        if before.phase is Phase.STOPPED:
            return before
        by_device = {intent.device_id: intent for intent in intents}
        await asyncio.gather(*(self._stop_device(d, by_device.get(d))
                               for d in before.devices if d in self.devices))
        current = self.manager.snapshot(session_id)
        if current.phase is Phase.STOPPED:
            return current
        try:
            return self.manager.finish_stop(session_id, current.revision)
        except ValueError:
            # A restore is still pending; do not release its lease.
            return self.manager.snapshot(session_id)

    async def _stop_device(self, device, intent):
        async with self._locks[device]:
            if intent is None or not self.manager.stop_intent_is_current(intent):
                return
            try:
                confirmed = await self.devices[device].adapter.apply_stop(intent)
            except Exception:
                confirmed = False
            if self.manager.stop_intent_is_current(intent):
                success = confirmed is True
                self.manager.acknowledge_stop(intent, success=success)
                self.status[device] = OutputStatus(
                    intent.session_id, intent.generation,
                    "stop_confirmed" if success else "stop_unconfirmed")
