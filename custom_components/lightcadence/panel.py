"""Same-origin, administrator-only HA panel; no credentials in static assets."""
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

MODULE_URL = "/lightcadence_assets/panel.js"


async def register_panel(hass):
    bundle = Path(__file__).parent / "frontend" / "panel.js"
    if not await hass.async_add_executor_job(bundle.is_file):
        # Fail setup explicitly instead of silently exposing a broken panel.
        return False
    await hass.http.async_register_static_paths([
        StaticPathConfig(MODULE_URL, str(bundle), False)
    ])
    await panel_custom.async_register_panel(
        hass, frontend_url_path=DOMAIN, webcomponent_name="lightcadence-panel",
        sidebar_title="LightCadence", sidebar_icon="mdi:lightbulb-group",
        module_url=MODULE_URL, require_admin=True,
    )
    return True
