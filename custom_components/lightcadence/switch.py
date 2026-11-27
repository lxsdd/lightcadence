"""Explicitly selected saved-show switches, not light entities or HA scenes."""
import asyncio
from datetime import timedelta
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from .client import EngineError
from .const import DOMAIN, CONF_SCENE_SWITCHES

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


def needs_audio(effect):
    """Saved effects never contain a persistent live source binding."""
    if isinstance(effect, dict):
        return effect.get("kind") == "audio_spectrum" or any(needs_audio(v) for v in effect.values())
    if isinstance(effect, list):
        return any(needs_audio(v) for v in effect)
    return False


def scene_options(state):
    lib = state.get("library", {})
    if lib.get("fault"):
        return {}
    return {s["id"]: s["name"] for s in lib.get("scenes", []) if not needs_audio(s["effect"])}


def matches(state, scene_id):
    return [s for s in state["sessions"] if s["phase"] != "stopped"
            and s.get("scene_origin", {}).get("scene_id") == scene_id]


async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    ids = entry.options.get(CONF_SCENE_SWITCHES, [])
    if not client.physical or not ids:
        return

    async def read_state():
        try:
            return await client.state()
        except EngineError as exc:
            raise UpdateFailed("Local engine state unavailable: " + exc.code) from None

    coordinator = DataUpdateCoordinator(hass, _LOGGER, name="LightCadence scene switches",
        config_entry=entry, update_method=read_state, update_interval=timedelta(seconds=5))
    await coordinator.async_config_entry_first_refresh()
    names = scene_options(coordinator.data)
    async_add_entities([SceneSwitch(coordinator, client, entry.entry_id, scene_id,
                                    names.get(scene_id, scene_id)) for scene_id in dict.fromkeys(ids)])


class SceneSwitch(CoordinatorEntity, SwitchEntity):
    _attr_icon = "mdi:lightbulb-group"

    def __init__(self, coordinator, client, entry_id, scene_id, name):
        super().__init__(coordinator)
        self.client = client
        self.scene_id = scene_id
        self._attr_unique_id = f"{entry_id}:{scene_id}"
        self._attr_name = f"LightCadence {name}"
        self._lock = asyncio.Lock()

    @property
    def available(self):
        if not super().available or not self.coordinator.data:
            return False
        state = self.coordinator.data
        active = matches(state, self.scene_id)
        return len(active) == 1 or (not active and self.scene_id in scene_options(state))

    @property
    def is_on(self):
        if not self.available:
            return None
        return bool(matches(self.coordinator.data, self.scene_id))

    async def _set(self, on):
        context = self._context
        async with self._lock:
            # Preserve caller context; never escalate a non-admin switch call.
            if context is not None and context.user_id is not None:
                user = await self.hass.auth.async_get_user(context.user_id)
                if user is None or not user.is_admin or not user.is_active:
                    raise HomeAssistantError("LightCadence action requires an administrator")
            try:
                state = await self.client.state()
            except EngineError as exc:
                await self.coordinator.async_refresh()
                raise HomeAssistantError("LightCadence state unavailable: " + exc.code) from None
            active = matches(state, self.scene_id)
            if len(active) > 1:
                raise ServiceValidationError("Ambiguous scene sessions; use individual panel stop")
            if (on and active) or (not on and not active):
                self.coordinator.async_set_updated_data(state)
                return
            if on and self.scene_id not in scene_options(state):
                raise ServiceValidationError("Scene missing or requires an explicit audio source")
            try:
                await self.hass.services.async_call(DOMAIN, "start_scene" if on else "stop_scene",
                    {"scene_id": self.scene_id}, blocking=True, context=context)
            finally:
                # Never optimistically claim a state or replay a write.
                await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
