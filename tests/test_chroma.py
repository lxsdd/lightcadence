"""Original Chroma adapter over real UDP sockets, synthetic loopback peer only."""
import asyncio
import json
import socket
import unittest

from engine.lightcadence_engine.chroma import ChromaError, ChromaV2Adapter, scene_parameters
from engine.lightcadence_engine.effects import Effect, EffectKind, LightTarget
from engine.lightcadence_engine.output import DeviceOutput, OutputCoordinator
from engine.lightcadence_engine.sessions import DeviceState, Frame, Phase, SessionManager, StopPolicy

TOKEN = "0123456789abcdef" * 2


class ChromaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.peer.bind(("127.0.0.1", 0))
        self.peer.setblocking(False)
        self.addCleanup(self.peer.close)
        self.at = 100.0
        self.adapter = ChromaV2Adapter("lamp", *self.peer.getsockname(),
                                      timeout=.15, clock=lambda: self.at)
        self.addCleanup(self.adapter.close)

    async def receive(self):
        async with asyncio.timeout(1):
            packet, addr = await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
        self.assertTrue(packet.endswith(b"\r\n"))
        return json.loads(packet), addr

    def reply(self, addr, obj):
        self.peer.sendto(json.dumps(obj).encode(), addr)

    async def acquire(self):
        task = asyncio.create_task(self.adapter.acquire())
        msg, addr = await self.receive()
        self.assertEqual(msg, {"id": msg["id"], "method": "udp_sess_new", "params": []})
        self.reply(addr, {"id": msg["id"], "method": "udp_sess_token",
                          "params": {"token": TOKEN}})
        await task
        self.assertTrue(self.adapter.ready)

    def frame(self, device="lamp"):
        return Frame("session", 1, device, 1, self.at, LightTarget(1, 0, .5, .5))

    async def test_actual_udp_scene_packet_and_renewal(self):
        await self.acquire()
        await self.adapter.send_frame(self.frame())
        msg, _ = await self.receive()
        self.assertEqual(msg["method"], "set_scene")
        self.assertEqual(msg["params"], ["color", 0xff00bc, 50, 0, "sudden"])
        self.assertEqual(msg["token"], TOKEN)
        self.at = 108
        task = asyncio.create_task(self.adapter.renew())
        keep, addr = await self.receive()
        self.assertEqual(keep["method"], "udp_sess_keep_alive")
        self.assertEqual(keep["params"], ["keeplive_interval", "10"])
        self.assertEqual(keep["token"], TOKEN)
        self.assertGreater(keep["id"], msg["id"])
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame())
        self.reply(addr, {"id": keep["id"], "token": TOKEN})
        await task
        self.at = 111
        self.assertTrue(self.adapter.ready)

    async def test_wrong_peer_and_request_id_cannot_acquire(self):
        task = asyncio.create_task(self.adapter.acquire())
        msg, addr = await self.receive()
        response = {"id": msg["id"], "method": "udp_sess_token",
                    "params": {"token": TOKEN}}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.sendto(json.dumps(response).encode(), addr)
        self.reply(addr, dict(response, id=msg["id"] + 1))
        with self.assertRaises(ChromaError):
            await task
        self.assertFalse(self.adapter.ready)

    async def test_invalid_token_and_renewal_denial_fail_closed(self):
        task = asyncio.create_task(self.adapter.acquire())
        msg, addr = await self.receive()
        self.reply(addr, {"id": msg["id"], "method": "udp_sess_token",
                          "params": {"token": "not-a-token"}})
        with self.assertRaises(ChromaError):
            await task
        self.assertFalse(self.adapter.ready)
        await self.acquire()
        task = asyncio.create_task(self.adapter.renew())
        msg, addr = await self.receive()
        self.reply(addr, {"id": msg["id"], "token": "f" * 32})
        with self.assertRaises(ChromaError):
            await task
        self.assertFalse(self.adapter.ready)
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame())

    async def test_expiry_and_wrong_device_do_not_transmit(self):
        await self.acquire()
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame("other"))
        self.at = 110
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame())
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)

    async def test_original_coordinator_uses_udp_but_retains_unconfirmed_restore(self):
        await self.acquire()
        manager = SessionManager(lambda: self.at)
        session = manager.start("r", "z", {"lamp": DeviceState(True, LightTarget(1, 0, 0, 1))},
                                Effect(EffectKind.RAINBOW))
        output = OutputCoordinator(manager, {"lamp": DeviceOutput(self.adapter, 0)},
                                   lambda: self.at)
        manager.render(session.id)
        output.pump()
        await output.drain()
        msg, _ = await self.receive()
        self.assertEqual(msg["params"], ["color", 0xff0000, 100, 0, "sudden"])
        self.assertEqual(output.status["lamp"].outcome, "submitted_unverified")
        stopped = await output.stop(session.id, session.revision)
        self.assertEqual(stopped.phase, Phase.STOPPING)
        self.assertEqual(manager.owner("lamp"), session.id)
        self.assertEqual(output.status["lamp"].outcome, "stop_unconfirmed")
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)

    def test_candidate_color_mapping_and_endpoint_validation(self):
        self.assertEqual(scene_parameters(LightTarget(1, 1, 1, 0)),
                         ["color", 0, 1, 0, "sudden"])
        for host in ("example.com", "8.8.8.8", "224.0.0.1", "192.168.1.255", "0.0.0.0"):
            with self.assertRaises(ValueError):
                ChromaV2Adapter("lamp", host)

    def coordinated(self):
        manager = SessionManager(lambda: self.at)
        session = manager.start("r", "z", {"lamp": DeviceState(True, LightTarget(1, 0, 0, 1))},
                                Effect(EffectKind.RAINBOW), StopPolicy.HOLD)
        output = OutputCoordinator(manager, {"lamp": self.adapter.output_profile(0)},
                                   lambda: self.at)
        return manager, session, output

    async def test_automatic_renewal_uses_latest_frame_after_handshake(self):
        await self.acquire()
        manager, session, output = self.coordinated()
        self.at = 108
        manager.render(session.id)
        output.pump()
        keep, addr = await self.receive()
        self.assertEqual(keep["method"], "udp_sess_keep_alive")
        self.at = 108.5
        manager.render(session.id)
        expected = session.effect.render(self.at - session.started_at, 0)
        self.reply(addr, {"id": keep["id"], "token": TOKEN})
        await output.drain()
        msg, _ = await self.receive()
        self.assertEqual(msg["method"], "set_scene")
        self.assertEqual(msg["params"], scene_parameters(expected))
        self.assertEqual(output.status["lamp"].sequence, 2)
        self.at = 118
        self.assertFalse(self.adapter.ready)

    async def test_stop_during_renewal_never_sends_queued_frame(self):
        await self.acquire()
        manager, session, output = self.coordinated()
        self.at = 108
        manager.render(session.id)
        output.pump()
        keep, addr = await self.receive()
        stop = asyncio.create_task(output.stop(session.id, session.revision))
        await asyncio.sleep(0)
        self.assertEqual(manager.snapshot(session.id).phase, Phase.STOPPING)
        self.reply(addr, {"id": keep["id"], "token": TOKEN})
        await output.drain()
        self.assertEqual((await stop).phase, Phase.STOPPED)
        self.assertFalse(self.adapter.ready)
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame())
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)

    async def test_handover_during_renewal_fences_queued_frame(self):
        await self.acquire()
        manager, session, output = self.coordinated()
        self.at = 108
        manager.render(session.id)
        output.pump()
        keep, addr = await self.receive()
        handover = asyncio.create_task(output.detach(session.id, session.revision, "lamp"))
        await asyncio.sleep(0)
        self.assertFalse(handover.done())
        self.reply(addr, {"id": keep["id"], "token": TOKEN})
        await output.drain()
        await handover
        self.assertIsNone(manager.owner("lamp"))
        self.assertFalse(self.adapter.ready)
        with self.assertRaises(ChromaError):
            await self.adapter.send_frame(self.frame())
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)

    async def test_late_renewal_does_not_reanimate_expired_session(self):
        await self.acquire()
        manager, session, output = self.coordinated()
        self.at = 108
        manager.render(session.id)
        output.pump()
        keep, addr = await self.receive()
        self.at = 110
        self.reply(addr, {"id": keep["id"], "token": TOKEN})
        await output.drain()
        self.assertFalse(self.adapter.ready)
        self.assertEqual(output.status["lamp"].outcome, "transport_error")
        manager.render(session.id)
        output.pump()
        await output.drain()
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.03):
                await asyncio.get_running_loop().sock_recvfrom(self.peer, 4096)
