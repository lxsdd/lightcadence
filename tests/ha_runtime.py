"""Real pinned HA runtime, real config flow and authenticated WebSocket test.

Only synthetic registry entries and test users; runs in a private, internal Docker network
container with a disposable configuration. Never loads a real light platform.
"""
import asyncio
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from aiohttp import ClientSession, web
from homeassistant import bootstrap, loader, runner
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.const import __version__ as HA_VERSION

from engine.lightcadence_engine.server import create_app

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
                hass.states.async_set(a.entity_id, "on", {"friendly_name": "Synthetic A"})
                hass.states.async_set(b.entity_id, "off", {"friendly_name": "Synthetic B"})
                async with ClientSession() as session:
                    async with await auth(session, admin) as ws:
                        panels = await command(ws, 100, "get_panels")
                        panel = panels["result"]["lightcadence"]
                        assert panel["config"]["_panel_custom"]["name"] == "lightcadence-panel"
                        assert panel["config"]["_panel_custom"]["module_url"] == "/lightcadence_assets/panel.js"
                        async with session.get(f"http://127.0.0.1:{HA_PORT}/lightcadence_assets/panel.js") as asset:
                            assert asset.status == 200
                            assert "lightcadence-panel" in await asset.text()
                        catalog = await command(ws, 1, "lightcadence/catalog")
                        assert catalog["success"], catalog
                        assert catalog["result"]["entries"] == [entry_id]
                        assert {x["id"] for x in catalog["result"]["lights"]} == {a.id, b.id}
                        assert not catalog["result"]["physical_output_enabled"]
                        renamed = registry.async_update_entity(a.entity_id, new_entity_id="light.synthetic_renamed")
                        assert renamed.id == a.id
                        catalog = await command(ws, 2, "lightcadence/catalog")
                        assert any(x["id"] == a.id and x["entity_id"] == renamed.entity_id
                                   for x in catalog["result"]["lights"])
                        payload = {"op": "start", "request_id": "ha-test", "zone_id": "test",
                                   "devices": [a.id, b.id], "effect": {"kind": "rainbow"}}
                        for bad in ("unknown", disabled.id, hidden.id, sensor.id):
                            denied = await command(ws, 3 + ("unknown", disabled.id, hidden.id, sensor.id).index(bad),
                                "lightcadence/preview", entry_id=entry_id,
                                payload={**payload, "devices": [a.id, bad]})
                            assert not denied["success"] and denied["error"]["code"] == "invalid_selection"
                        empty = await command(ws, 7, "lightcadence/state", entry_id=entry_id)
                        assert empty["result"]["sessions"] == []
                        started = await command(ws, 8, "lightcadence/preview", entry_id=entry_id, payload=payload)
                        assert started["success"], started
                        snapshot = started["result"]["sessions"][0]
                        assert set(snapshot["devices"]) == {a.id, b.id}
                        assert started["result"]["physical_output_enabled"] is False
                    async with await auth(session, ordinary) as ws:
                        panels = await command(ws, 100, "get_panels")
                        assert "lightcadence" not in panels["result"]
                        for i, kind, args in [
                            (1, "lightcadence/catalog", {}),
                            (2, "lightcadence/state", {"entry_id": entry_id}),
                            (3, "lightcadence/preview", {"entry_id": entry_id, "payload": payload}),
                        ]:
                            denied = await command(ws, i, kind, **args)
                            assert not denied["success"], denied
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
                print("HA_RUNTIME_CONFIG_FLOW_AUTH_REGISTRY_RENAME_RECONNECT_STOP=PASS\\nHA_RUNTIME_ADMIN_PANEL_ASSET=PASS")
            finally:
                await hass.async_stop()
    finally:
        await engine.cleanup()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=runner.create_event_loop)
