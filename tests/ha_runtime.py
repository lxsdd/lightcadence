"""Real pinned HA runtime, real config flow and authenticated WebSocket test.

Only synthetic registry entries and test users; runs in a private, internal Docker network
container with a disposable configuration. Never loads a real light platform.
"""
import asyncio
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4
from types import SimpleNamespace

from aiohttp import ClientSession, web
from homeassistant import bootstrap, loader, runner
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.const import __version__ as HA_VERSION

from engine.lightcadence_engine.server import create_app
from engine.lightcadence_engine.runtime import PhysicalRuntime
from engine.lightcadence_engine.enrollment import ChromaProfile, Observation
from engine.lightcadence_engine.yeelight_lan import _decode
from test_yeelight_lan import ChromaPeer

TOKEN = "synthetic_only_" + uuid4().hex
PORT = 17842
HA_PORT = 18123
ROOT = Path(__file__).resolve().parents[1]


async def auth(session, token):
    ws = await session.ws_connect(f"http://127.0.0.1:{HA_PORT}/api/websocket")
    assert (await ws.receive_json())["type"] == "auth_required"
    await ws.send_json({"type": "auth", "access_token": token})
    assert (await ws.receive_json())["type"] == "auth_ok"
    return ws


async def command(ws, msg_id, kind, **kwargs):
    await ws.send_json({"id": msg_id, "type": kind, **kwargs})
    result = await ws.receive_json(timeout=10)
    assert result["id"] == msg_id and result["type"] == "result", result
    return result


async def issue_token(hass, name, group):
    user = await hass.auth.async_create_user(name, group_ids=[group])
    refresh = await hass.auth.async_create_refresh_token(user, client_id="http://localhost/")
    return hass.auth.async_create_access_token(refresh)


class SyntheticStatePort:
    def __init__(self, ids):
        self.states = {d: _decode("on", 60, 0xff0000) for d in ids}
        self.writes = []

    async def read(self, profile):
        return Observation(profile.device_id, profile.ha_registry_id, profile.model,
                           profile.firmware, "rgb", self.states[profile.device_id])

    async def write(self, profile, state):
        self.writes.append((profile.device_id, state))
        self.states[profile.device_id] = state
        return True


async def physical_path(hass, session, admin, ordinary, old_entry, registry, a, b):
    # Real HA WebSocket/config flow, original physical runtime, loopback peers,
    # synthetic state authority only. No real HA light platform or hardware.
    await hass.config_entries.async_remove(old_entry)
    registry.async_update_entity(a.entity_id, disabled_by=None)
    c = registry.async_get_or_create("light", "demo", "not-enrolled",
                                    suggested_object_id="not_enrolled")
    fixture = SimpleNamespace(chroma_messages=[])
    peers = []
    engine = None
    try:
        for _ in (a, b):
            peer, _protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: ChromaPeer(fixture), local_addr=("127.0.0.1", 0))
            peers.append(peer)
        profiles = [ChromaProfile(d.id, d.id, "synthetic", "1", "127.0.0.1", .03,
                                  peer.get_extra_info("sockname")[1])
                    for d, peer in zip((a, b), peers, strict=True)]
        authority = SyntheticStatePort([a.id, b.id])
        runtime = PhysicalRuntime(profiles, authority, enabled=True)
        engine = web.AppRunner(create_app(TOKEN, runtime), access_log=None)
        await engine.setup()
        port = 17843
        await web.TCPSite(engine, "127.0.0.1", port).start()
        flow = await hass.config_entries.flow.async_init("lightcadence", context={"source": "user"})
        assert flow["type"] == "form", flow
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"],
            {"port": port, "token": TOKEN, "physical_candidate": False})
        assert flow["errors"] == {"base": "cannot_connect"}, flow
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"],
            {"port": port, "token": TOKEN, "physical_candidate": True})
        assert flow["type"] == "create_entry", flow
        entry_id = flow["result"].entry_id
        await hass.async_block_till_done()
        assert flow["result"].state.value == "loaded"
        start = {"op": "start", "request_id": "physical", "zone_id": "two-lamp",
                 "devices": [a.id, b.id], "effect": {"kind": "rainbow"},
                 "instance_id": runtime.instance_id}
        async with await auth(session, ordinary) as ws:
            denied = await command(ws, 1, "lightcadence/physical", entry_id=entry_id, payload=start)
            assert not denied["success"] and denied["error"]["code"] == "unauthorized", denied
        async with await auth(session, admin) as ws:
            cat = await command(ws, 1, "lightcadence/catalog")
            assert cat["success"] and cat["result"]["physical_output_enabled"], cat
            assert {l["id"] for l in cat["result"]["lights"]} == {a.id, b.id}
            denied = await command(ws, 2, "lightcadence/preview", entry_id=entry_id, payload=start)
            assert not denied["success"] and denied["error"]["code"] == "incompatible_engine"
            denied = await command(ws, 3, "lightcadence/physical", entry_id=entry_id,
                                   payload={**start, "instance_id": "old"})
            assert not denied["success"] and denied["error"]["code"] == "stale_instance"
            denied = await command(ws, 4, "lightcadence/physical", entry_id=entry_id,
                                   payload={**start, "devices": [c.id]})
            assert not denied["success"] and denied["error"]["code"] == "invalid_request"
            assert fixture.chroma_messages == [] and not authority.writes
            began = await command(ws, 5, "lightcadence/physical", entry_id=entry_id, payload=start)
            assert began["success"], began
            s = began["result"]["sessions"][0]
            assert set(s["devices"]) == {a.id, b.id} and s["phase"] == "running"
            manual = {"op": "manual_set", "request_id": "rgb", "device_id": a.id,
                      "instance_id": runtime.instance_id,
                      "state": {"power": True, "rgb": [64, 128, 255], "brightness_pct": 71}}
            result = await command(ws, 6, "lightcadence/physical", entry_id=entry_id, payload=manual)
            assert result["success"] and result["result"]["manual_result"] == "confirmed", result
            after = result["result"]["sessions"][0]
            assert after["devices"] == [b.id] and after["phase"] == "running"
            replay = await command(ws, 7, "lightcadence/physical", entry_id=entry_id, payload=manual)
            assert replay["success"] and len(authority.writes) == 1
            assert authority.states[a.id] == _decode("on", 71, 0x4080ff)
            off = {"op": "manual_off", "request_id": "off", "device_id": a.id,
                   "instance_id": runtime.instance_id}
            result = await command(ws, 8, "lightcadence/physical", entry_id=entry_id, payload=off)
            assert result["success"] and result["result"]["manual_result"] == "confirmed", result
            assert authority.states[a.id] == _decode("off", 71, 0x4080ff)
            stopped = await command(ws, 9, "lightcadence/physical", entry_id=entry_id,
                payload={"op": "stop", "session_id": s["id"], "revision": after["revision"],
                         "instance_id": runtime.instance_id})
            assert stopped["success"] and stopped["result"]["sessions"][0]["phase"] == "stopped", stopped
            assert authority.states[a.id] == _decode("off", 71, 0x4080ff)
            assert [d for d, _ in authority.writes].count(a.id) == 2
        assert await hass.config_entries.async_unload(entry_id)
    finally:
        if engine is not None:
            await engine.cleanup()
        for peer in peers:
            peer.close()
    print("HA_RUNTIME_PHYSICAL_MODE_COOPERATIVE_HANDOVER=PASS")


async def main():
    assert HA_VERSION == "2026.11.2", HA_VERSION
    engine = web.AppRunner(create_app(TOKEN), access_log=None)
    await engine.setup()
    await web.TCPSite(engine, "127.0.0.1", PORT).start()
    try:
        with tempfile.TemporaryDirectory(prefix="lightcadence-ha-") as config:
            shutil.copytree(ROOT / "custom_components" / "lightcadence",
                            Path(config) / "custom_components" / "lightcadence")
            hass = HomeAssistant(config)
            loader.async_setup(hass)
            hass.config.skip_pip = True
            try:
                result = await bootstrap.async_from_config_dict({
                    "homeassistant": {"name": "Synthetic CI", "latitude": 0, "longitude": 0,
                                      "elevation": 0, "unit_system": "metric", "time_zone": "UTC"},
                    "http": {"server_host": "127.0.0.1", "server_port": HA_PORT},
                    "websocket_api": {}, "config": {}, "frontend": {},
                }, hass)
                assert result is hass, "HA bootstrap failed"
                await hass.async_start()
                admin = await issue_token(hass, "Synthetic admin", GROUP_ID_ADMIN)
                ordinary = await issue_token(hass, "Synthetic user", GROUP_ID_USER)
                flow = await hass.config_entries.flow.async_init("lightcadence", context={"source": "user"})
                assert flow["type"] == "form", flow["type"]
                flow = await hass.config_entries.flow.async_configure(flow["flow_id"],
                    {"port": PORT, "token": "wrong_" + "x" * 32})
                assert flow["errors"] == {"base": "invalid_auth"}
                flow = await hass.config_entries.flow.async_configure(flow["flow_id"],
                    {"port": PORT, "token": TOKEN})
                assert flow["type"] == "create_entry", flow["type"]
                entry_id = flow["result"].entry_id
                await hass.async_block_till_done()
                assert flow["result"].state.value == "loaded", flow["result"].state
                duplicate = await hass.config_entries.flow.async_init("lightcadence", context={"source": "user"})
                assert duplicate["type"] == "abort"

                registry = er.async_get(hass)
                a = registry.async_get_or_create("light", "demo", "synthetic-a",
                                                 suggested_object_id="synthetic_a")
                b = registry.async_get_or_create("light", "demo", "synthetic-b",
                                                 suggested_object_id="synthetic_b")
                disabled = registry.async_get_or_create("light", "demo", "synthetic-disabled",
                    suggested_object_id="synthetic_disabled", disabled_by=er.RegistryEntryDisabler.USER)
                hidden = registry.async_get_or_create("light", "demo", "synthetic-hidden",
                    suggested_object_id="synthetic_hidden", hidden_by=er.RegistryEntryHider.USER)
                sensor = registry.async_get_or_create("sensor", "demo", "synthetic-sensor",
                                                      suggested_object_id="synthetic_sensor")
                hass.states.async_set(a.entity_id, "on", {"friendly_name": "Synthetic A",
                    "color_mode": "rgb", "rgb_color": (64, 128, 255), "brightness": 120})
                hass.states.async_set(b.entity_id, "off", {"friendly_name": "Synthetic B"})
                async with ClientSession() as session:
                    async with await auth(session, admin) as ws:
                        panels = await command(ws, 1, "get_panels")
                        panel = panels["result"]["lightcadence"]
                        assert panel["config"]["_panel_custom"]["name"] == "lightcadence-panel"
                        assert panel["config"]["_panel_custom"]["module_url"] == "/lightcadence_assets/panel.js"
                        async with session.get(f"http://127.0.0.1:{HA_PORT}/lightcadence_assets/panel.js") as asset:
                            assert asset.status == 200
                            assert "lightcadence-panel" in await asset.text()
                        catalog = await command(ws, 2, "lightcadence/catalog")
                        assert catalog["success"], catalog
                        assert catalog["result"]["entries"] == [entry_id]
                        assert {x["id"] for x in catalog["result"]["lights"]} == {a.id, b.id}
                        assert not catalog["result"]["physical_output_enabled"]
                        by_id = {x["id"]: x["observation"] for x in catalog["result"]["lights"]}
                        assert by_id[a.id] == {"source": "ha_cache", "status": "cached_rgb",
                            "power": True, "color_mode": "rgb", "rgb": [64, 128, 255], "brightness": 120}
                        assert by_id[b.id]["power"] is False
                        assert by_id[b.id]["rgb"] is None
                        assert by_id[b.id]["status"] == "unsupported_or_unknown_mode"
                        from custom_components.lightcadence.catalog import cached_light_state
                        hass.states.async_set(b.entity_id, "on", {"color_mode": "rgb",
                            "rgb_color": (0, 1, 2), "brightness": 10, "effect": "rainbow"})
                        assert cached_light_state(hass.states.get(b.entity_id))["status"] == "effect_active"
                        hass.states.async_set(b.entity_id, "on", {"color_mode": "color_temp",
                            "rgb_color": (0, 1, 2), "brightness": 10})
                        assert cached_light_state(hass.states.get(b.entity_id))["rgb"] is None
                        hass.states.async_set(b.entity_id, "on", {"color_mode": "rgb", "brightness": 10})
                        assert cached_light_state(hass.states.get(b.entity_id))["status"] == "incomplete_rgb"
                        hass.states.async_set(b.entity_id, "unavailable")
                        assert cached_light_state(hass.states.get(b.entity_id))["power"] is None
                        renamed = registry.async_update_entity(a.entity_id, new_entity_id="light.synthetic_renamed")
                        assert renamed.id == a.id
                        catalog = await command(ws, 3, "lightcadence/catalog")
                        assert any(x["id"] == a.id and x["entity_id"] == renamed.entity_id
                                   for x in catalog["result"]["lights"])
                        payload = {"op": "start", "request_id": "ha-test", "zone_id": "test",
                                   "devices": [a.id, b.id], "effect": {"kind": "rainbow"}}
                        for bad in ("unknown", disabled.id, hidden.id, sensor.id):
                            denied = await command(ws, 4 + ("unknown", disabled.id, hidden.id, sensor.id).index(bad),
                                "lightcadence/preview", entry_id=entry_id,
                                payload={**payload, "devices": [a.id, bad]})
                            assert not denied["success"] and denied["error"]["code"] == "invalid_selection"
                        refused = await command(ws, 8, "lightcadence/physical", entry_id=entry_id, payload=payload)
                        assert not refused["success"] and refused["error"]["code"] == "incompatible_engine"
                        empty = await command(ws, 9, "lightcadence/state", entry_id=entry_id)
                        assert empty["result"]["sessions"] == []
                        started = await command(ws, 10, "lightcadence/preview", entry_id=entry_id, payload=payload)
                        assert started["success"], started
                        snapshot = started["result"]["sessions"][0]
                        assert set(snapshot["devices"]) == {a.id, b.id}
                        assert started["result"]["physical_output_enabled"] is False
                    async with await auth(session, ordinary) as ws:
                        panels = await command(ws, 1, "get_panels")
                        assert "lightcadence" not in panels["result"]
                        for i, kind, args in [
                            (2, "lightcadence/catalog", {}),
                            (3, "lightcadence/state", {"entry_id": entry_id}),
                            (4, "lightcadence/preview", {"entry_id": entry_id, "payload": payload}),
                            (5, "lightcadence/physical", {"entry_id": entry_id, "payload": payload}),
                        ]:
                            denied = await command(ws, i, kind, **args)
                            assert not denied["success"] and denied["error"]["code"] == "unauthorized", denied
                    async with await auth(session, admin) as ws:
                        reconnect = await command(ws, 1, "lightcadence/state", entry_id=entry_id)
                        assert reconnect["result"]["sessions"] == [snapshot]
                        # Disable a selected light while preview is running.
                        # Stop must remain possible after the catalog changes.
                        registry.async_update_entity(renamed.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
                        stopped = await command(ws, 2, "lightcadence/preview", entry_id=entry_id,
                            payload={"op": "stop", "session_id": snapshot["id"], "revision": snapshot["revision"]})
                        assert stopped["success"] and stopped["result"]["sessions"][0]["phase"] == "stopped"
                        assert stopped["result"]["targets"] == {}
                        assert await hass.config_entries.async_unload(entry_id)
                        missing = await command(ws, 3, "lightcadence/state", entry_id=entry_id)
                        assert not missing["success"] and missing["error"]["code"] == "not_found"
                    await physical_path(hass, session, admin, ordinary, entry_id, registry, renamed, b)
                print("HA_RUNTIME_CONFIG_FLOW_AUTH_REGISTRY_RENAME_RECONNECT_STOP=PASS\\nHA_RUNTIME_ADMIN_PANEL_ASSET=PASS")
            finally:
                await hass.async_stop()
    finally:
        await engine.cleanup()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=runner.create_event_loop)
