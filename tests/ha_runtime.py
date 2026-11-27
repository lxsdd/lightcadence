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
from homeassistant.exceptions import HomeAssistantError
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
            denied = await command(ws, 2, "call_service", domain="lightcadence", service="turn_off",
                                   service_data={"entity_id": a.entity_id})
            assert not denied["success"], denied
            assert not authority.writes
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
            # An explicit HA automation action uses the same engine reservation,
            # not a second LAN writer. Invalid and unenrolled actions do not detach.
            for service, data in (("turn_on", {"entity_id": c.entity_id}),
                                  ("set_rgb", {"entity_id": b.entity_id, "rgb_color": [True, 0, 0], "brightness_pct": 40})):
                denied = await command(ws, 9 if service == "turn_on" else 10, "call_service",
                                       domain="lightcadence", service=service, service_data=data)
                assert not denied["success"], denied
            assert runtime.manager.owner(b.id) == s["id"]
            assert len(authority.writes) == 2
            turned_on = await command(ws, 11, "call_service", domain="lightcadence", service="turn_on",
                                      service_data={"entity_id": a.entity_id})
            assert turned_on["success"], turned_on
            assert authority.states[a.id] == _decode("on", 71, 0x4080ff)
            await hass.services.async_call("lightcadence", "set_rgb",
                {"entity_id": b.entity_id, "rgb_color": [16, 32, 64], "brightness_pct": 45},
                blocking=True)
            assert authority.states[b.id] == _decode("on", 45, 0x102040)
            assert runtime.manager.owner(b.id) is None
            assert runtime.manager.snapshot(s["id"]).phase.value == "stopped"
            stopped = await command(ws, 12, "lightcadence/physical", entry_id=entry_id,
                payload={"op": "stop", "session_id": s["id"], "revision": runtime.manager.snapshot(s["id"]).revision,
                         "instance_id": runtime.instance_id})
            assert stopped["success"] and stopped["result"]["sessions"][0]["phase"] == "stopped", stopped
            assert authority.states[a.id] == _decode("on", 71, 0x4080ff)
            assert authority.states[b.id] == _decode("on", 45, 0x102040)
            assert [d for d, _ in authority.writes].count(a.id) == 3
            assert [d for d, _ in authority.writes].count(b.id) == 1
            # Saved scene actions run through the real HA service registry
            # and the original physical admission/stop transaction.
            zone = runtime.library
            zone.command({"op": "library_save", "revision": 0, "kind": "zones", "id": None,
                          "value": {"name": "Synthetic automation", "devices": [a.id]}})
            zid = zone.snapshot()["zones"][0]["id"]
            zone.command({"op": "library_save", "revision": 1, "kind": "scenes", "id": None,
                          "value": {"name": "Synthetic saved", "zone_id": zid,
                                    "effect": {"kind": "static", "period_seconds": 4, "brightness": .5,
                                               "color": [1,0,0], "width": .25}, "stop_policy": "restore"}})
            saved_id = zone.snapshot()["scenes"][0]["id"]
            before = len(authority.writes)
            original_a = authority.states[a.id]
            async with await auth(session, ordinary) as nonadmin:
                refused = await command(nonadmin, 1, "call_service", domain="lightcadence",
                    service="start_scene", service_data={"scene_id": saved_id})
                assert not refused["success"], refused
            assert runtime.manager.owner(a.id) is None
            denied = await command(ws, 13, "call_service", domain="lightcadence",
                service="start_scene", service_data={"scene_id": "unknown"})
            assert not denied["success"], denied
            assert len(authority.writes) == before
            await hass.services.async_call("lightcadence", "start_scene", {"scene_id": saved_id}, blocking=True)
            owner = runtime.manager.owner(a.id)
            assert owner is not None
            snapshot = next(s for s in runtime.state()["sessions"] if s["id"] == owner)
            assert snapshot["scene_origin"]["scene_id"] == saved_id
            # Library deletion must not prevent stopping an already started scene.
            zone.command({"op": "library_delete", "revision": 2, "kind": "scenes",
                          "id": saved_id, "value": None})
            stopped = await command(ws, 14, "call_service", domain="lightcadence",
                service="stop_scene", service_data={"scene_id": saved_id})
            assert stopped["success"], stopped
            assert runtime.manager.owner(a.id) is None
            assert authority.states[a.id] == original_a
            assert len(authority.writes) == before + 1
            await hass.services.async_call("lightcadence", "stop_scene", {"scene_id": saved_id}, blocking=True)
            assert len(authority.writes) == before + 1
        assert await hass.config_entries.async_unload(entry_id)
        try:
            await hass.services.async_call("lightcadence", "turn_off", {"entity_id": a.entity_id}, blocking=True)
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("unloaded engine accepted an action")
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
                    "scene": [{"id": "synthetic_rgb_import", "name": "Synthetic RGB import",
                               "entities": {"light.synthetic_b": {"state": "on", "color_mode": "rgb", "rgb_color": [64, 128, 255], "brightness": 120}}},
                              {"id": "synthetic_off_import", "name": "Synthetic off import",
                               "entities": {"light.synthetic_b": {"state": "off"}}}],
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
                        try:
                            await hass.services.async_call("lightcadence", "turn_off", {"entity_id": renamed.entity_id}, blocking=True)
                        except HomeAssistantError:
                            pass
                        else:
                            raise AssertionError("preview accepted physical action")
                        refused = await command(ws, 8, "lightcadence/physical", entry_id=entry_id, payload=payload)
                        assert not refused["success"] and refused["error"]["code"] == "incompatible_engine"
                        empty = await command(ws, 9, "lightcadence/state", entry_id=entry_id)
                        assert empty["result"]["sessions"] == []
                        started = await command(ws, 10, "lightcadence/preview", entry_id=entry_id, payload=payload)
                        assert started["success"], started
                        snapshot = started["result"]["sessions"][0]
                        assert set(snapshot["devices"]) == {a.id, b.id}
                        assert started["result"]["physical_output_enabled"] is False
                        # Geometry and zones use the original admin bridge and
                        # stable registry references, including renamed lights.
                        instance = empty["result"]["instance_id"]
                        saved = await command(ws, 11, "lightcadence/preview", entry_id=entry_id,
                            payload={"op": "library_save", "instance_id": instance, "revision": 0,
                                     "kind": "zones", "id": None, "value": {"name": "Synthetic spatial zone", "devices": [a.id, b.id]}})
                        assert saved["success"], saved
                        zid = saved["result"]["library"]["zones"][0]["id"]
                        def feature(id, kind, points, device=None, zone=None):
                            return {"id": id, "kind": kind, "name": id, "points": points,
                                    "device_id": device, "zone_id": zone}
                        floor = {"op": "library_save", "instance_id": instance, "revision": 1, "kind": "floorplans",
                                 "id": None, "value": {"name": "Synthetic floor", "width": 10, "height": 8,
                                 "features": [feature("a", "lamp", [[0,0]], a.id),
                                              feature("b", "lamp", [[8,0]], b.id),
                                              feature("path", "effect_path", [[0,0],[10,0]], zone=zid)]}}
                        for n, bad in enumerate((disabled.id, hidden.id, sensor.id, "unknown")):
                            invalid = {**floor, "value": {**floor["value"], "features":
                                [feature("bad", "lamp", [[0,0]], bad)]}}
                            denied = await command(ws, 12+n, "lightcadence/preview", entry_id=entry_id, payload=invalid)
                            assert not denied["success"] and denied["error"]["code"] == "invalid_selection", denied
                        saved = await command(ws, 16, "lightcadence/preview", entry_id=entry_id, payload=floor)
                        assert saved["success"], saved
                        assert saved["result"]["library"]["spatial_paths"][0]["positions"] == {a.id: 0., b.id: .8}
                        assert saved["result"]["sessions"] == [snapshot]
                        stale = await command(ws, 17, "lightcadence/preview", entry_id=entry_id, payload=floor)
                        assert not stale["success"] and stale["error"]["code"] == "stale_revision", stale
                        scenes = await command(ws, 18, "lightcadence/ha_scenes")
                        assert scenes["success"], scenes
                        native = {row["entity_id"]: row for row in scenes["result"]}
                        rgb_id = native["scene.synthetic_rgb_import"]["id"]
                        off_id = native["scene.synthetic_off_import"]["id"]
                        before_b = hass.states.get(b.entity_id)
                        inspection = await command(ws, 19, "lightcadence/ha_scene_inspect", entry_id=entry_id, scene_id=rgb_id)
                        assert inspection["success"], inspection
                        target = inspection["result"]
                        assert target["importable"] and target["targets"] == [{"device_id": b.id, "entity_id": b.entity_id, "rgb": [64,128,255], "brightness": 120}], target
                        unsupported = await command(ws, 20, "lightcadence/ha_scene_inspect", entry_id=entry_id, scene_id=off_id)
                        assert unsupported["success"] and not unsupported["result"]["importable"], unsupported
                        args = {"entry_id": entry_id, "scene_id": rgb_id, "fingerprint": target["fingerprint"],
                                "instance_id": instance, "revision": 2, "name": "Imported synthetic RGB"}
                        changed = await command(ws, 21, "lightcadence/ha_scene_import", **{**args, "fingerprint": "0"*64})
                        assert not changed["success"] and changed["error"]["code"] == "stale_revision", changed
                        denied_off = await command(ws, 22, "lightcadence/ha_scene_import", **{**args, "scene_id": off_id, "fingerprint": unsupported["result"]["fingerprint"]})
                        assert not denied_off["success"], denied_off
                        imported = await command(ws, 23, "lightcadence/ha_scene_import", **args)
                        assert imported["success"], imported
                        lib = imported["result"]["library"]
                        assert lib["revision"] == 3 and len(lib["zones"]) == 2 and len(lib["scenes"]) == 1
                        assert lib["scenes"][0]["ha_source"]["registry_id"] == rgb_id
                        assert lib["zones"][-1]["devices"] == [b.id]
                        assert imported["result"]["sessions"] == [snapshot]
                        assert hass.states.get(b.entity_id) == before_b
                        repeated = await command(ws, 24, "lightcadence/ha_scene_import", **args)
                        assert not repeated["success"] and repeated["error"]["code"] == "stale_revision"
                    async with await auth(session, ordinary) as ws:
                        panels = await command(ws, 1, "get_panels")
                        assert "lightcadence" not in panels["result"]
                        for i, kind, args in [
                            (2, "lightcadence/catalog", {}),
                            (3, "lightcadence/state", {"entry_id": entry_id}),
                            (4, "lightcadence/preview", {"entry_id": entry_id, "payload": payload}),
                            (5, "lightcadence/physical", {"entry_id": entry_id, "payload": payload}),
                            (6, "lightcadence/preview", {"entry_id": entry_id, "payload": floor}),
                            (7, "lightcadence/ha_scenes", {}),
                            (8, "lightcadence/ha_scene_inspect", {"entry_id": entry_id, "scene_id": rgb_id}),
                            (9, "lightcadence/ha_scene_import", args),
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
