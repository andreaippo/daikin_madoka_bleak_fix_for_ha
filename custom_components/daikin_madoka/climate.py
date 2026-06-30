"""Support for the Daikin Madoka HVAC."""
import logging
import asyncio

from pymadoka import (
    ConnectionException,
    Controller,
    FanSpeedEnum,
    FanSpeedStatus,
    OperationModeEnum,
    OperationModeStatus,
    PowerStateStatus,
    SetPointStatus,
)
from pymadoka.connection import ConnectionStatus
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from . import DOMAIN
from .const import CONTROLLERS, MAX_TEMP, MIN_TEMP

_LOGGER = logging.getLogger(__name__)

HA_MODE_TO_DAIKIN = {
    HVACMode.FAN_ONLY: OperationModeEnum.FAN,
    HVACMode.DRY: OperationModeEnum.DRY,
    HVACMode.COOL: OperationModeEnum.COOL,
    HVACMode.HEAT: OperationModeEnum.HEAT,
    HVACMode.AUTO: OperationModeEnum.AUTO,
    HVACMode.OFF: OperationModeEnum.AUTO,
}

DAIKIN_TO_HA_MODE = {
    OperationModeEnum.FAN: HVACMode.FAN_ONLY,
    OperationModeEnum.DRY: HVACMode.DRY,
    OperationModeEnum.COOL: HVACMode.COOL,
    OperationModeEnum.HEAT: HVACMode.HEAT,
    OperationModeEnum.AUTO: HVACMode.AUTO,
}

HA_FAN_MODE_TO_DAIKIN = {
    FAN_LOW: FanSpeedEnum.LOW,
    FAN_MEDIUM: FanSpeedEnum.MID,
    FAN_HIGH: FanSpeedEnum.HIGH,
    FAN_AUTO: FanSpeedEnum.AUTO,
}

DAIKIN_TO_HA_FAN_MODE = {
    FanSpeedEnum.LOW: FAN_LOW,
    FanSpeedEnum.MID: FAN_MEDIUM,
    FanSpeedEnum.HIGH: FAN_HIGH,
    FanSpeedEnum.AUTO: FAN_AUTO,
}

DAIKIN_TO_HA_CURRENT_HVAC_MODE = {
    OperationModeEnum.FAN: HVACAction.FAN,
    OperationModeEnum.DRY: HVACAction.DRYING,
    OperationModeEnum.COOL: HVACAction.COOLING,
    OperationModeEnum.HEAT: HVACAction.HEATING,
}

DATA = "data"

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Daikin climate based on config_entry."""
    if entry.entry_id in hass.data[DOMAIN]:
        entities = []
        for controller in hass.data[DOMAIN][entry.entry_id][CONTROLLERS].values():
            try:
                entity = DaikinMadokaClimate(controller)
                entities.append(entity)
                await entity.controller.update()
            except ConnectionAbortedError:
                pass
            except ConnectionException:
                pass

        async_add_entities(entities, update_before_add=True)

class DaikinMadokaClimate(ClimateEntity):
    """Representation of a Daikin HVAC."""

    def __init__(self, controller: Controller):
        """Initialize the climate device."""
        self.controller = controller
        self.dev_info = None
        self._lock = asyncio.Lock()

    @property
    def supported_features(self):
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )

    @property
    def available(self):
        return self.controller.connection.connection_status == ConnectionStatus.CONNECTED

    @property
    def name(self):
        return (
            self.controller.connection.name
            if self.controller.connection.name is not None
            else self.controller.connection.address
        )

    @property
    def unique_id(self):
        return self.controller.connection.address

    @property
    def temperature_unit(self):
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self):
        if self.controller.temperatures.status is None:
            return None
        return self.controller.temperatures.status.indoor

    @property
    def target_temperature(self):
        if self.controller.set_point.status is None:
            return None
        if self.hvac_mode == HVACMode.HEAT:
            return self.controller.set_point.status.heating_set_point
        return self.controller.set_point.status.cooling_set_point

    @property
    def target_temperature_step(self):
        return 1

    @property
    def min_temp(self):
        return MIN_TEMP

    @property
    def max_temp(self):
        return MAX_TEMP

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature with safety locks."""
        if self._lock.locked():
            _LOGGER.warning("DEBUG_MADOKA: Ignorato comando su %s, BUSY", self.name)
            return

        async with self._lock:
            try:
                if self.controller.set_point.status is None or self.controller.operation_mode.status is None:
                    return

                target_temperature = kwargs.get(ATTR_TEMPERATURE)
                if target_temperature is None:
                    return

                # Recupera valori attuali e valida (fallback a 22 se <= 0)
                curr_c = self.controller.set_point.status.cooling_set_point
                curr_h = self.controller.set_point.status.heating_set_point
                
                new_cooling = curr_c if curr_c > 15 else 22
                new_heating = curr_h if curr_h > 15 else 22

                if self.controller.operation_mode.status.operation_mode != OperationModeEnum.HEAT:
                    new_cooling = round(target_temperature)
                if self.controller.operation_mode.status.operation_mode != OperationModeEnum.COOL:
                    new_heating = round(target_temperature)

                _LOGGER.warning("DEBUG_MADOKA: Invio dati -> C:%s, H:%s", new_cooling, new_heating)
                
                await self.controller.set_point.update(SetPointStatus(new_cooling, new_heating))
                await asyncio.sleep(0.5) # Ritardo BLE

            except (ConnectionAbortedError, ConnectionException):
                _LOGGER.warning("Could not set target temperature on %s.", self.name)

    @property
    def hvac_mode(self):
        if self.controller.power_state.status is None or self.controller.operation_mode.status is None:
            return None
        if not self.controller.power_state.status.turn_on:
            return HVACMode.OFF
        return DAIKIN_TO_HA_MODE.get(self.controller.operation_mode.status.operation_mode)

    @property
    def hvac_modes(self):
        return list(HA_MODE_TO_DAIKIN)

    @property
    def hvac_action(self):
        if self.controller.power_state.status is None or self.controller.operation_mode.status is None:
            return None
        if not self.controller.power_state.status.turn_on:
            return HVACAction.OFF
        if self.controller.operation_mode.status.operation_mode == OperationModeEnum.AUTO:
            if self.target_temperature is None or self.current_temperature is None:
                return None
            return HVACAction.HEATING if self.target_temperature >= self.current_temperature else HVACAction.COOLING
        return DAIKIN_TO_HA_CURRENT_HVAC_MODE.get(self.controller.operation_mode.status.operation_mode)

    async def async_set_hvac_mode(self, hvac_mode):
        try:
            if hvac_mode != HVACMode.OFF:
                await self.controller.operation_mode.update(OperationModeStatus(HA_MODE_TO_DAIKIN.get(hvac_mode)))
            await self.controller.power_state.update(PowerStateStatus(hvac_mode != HVACMode.OFF))
            self.async_schedule_update_ha_state()
        except (ConnectionAbortedError, ConnectionException):
            _LOGGER.warning("Could not set HVAC mode on %s.", self.name)

    @property
    def fan_mode(self):
        if self.controller.fan_speed.status is None: return None
        mode = self.controller.fan_speed.status.heating_fan_speed if self.hvac_mode == HVACMode.HEAT else self.controller.fan_speed.status.cooling_fan_speed
        return DAIKIN_TO_HA_FAN_MODE.get(mode)

    async def async_set_fan_mode(self, fan_mode):
        try:
            val = HA_FAN_MODE_TO_DAIKIN.get(fan_mode)
            await self.controller.fan_speed.update(FanSpeedStatus(val, val))
        except (ConnectionAbortedError, ConnectionException):
            _LOGGER.warning("Could not set fan mode on %s.", self.name)

    @property
    def fan_modes(self):
        return list(HA_FAN_MODE_TO_DAIKIN)

    async def async_update(self):
        try:
            self.dev_info = await self.controller.read_info()
            await self.controller.update()
        except (ConnectionAbortedError, ConnectionException):
            _LOGGER.warning("Could not update device status for %s.", self.name)

    async def async_turn_on(self):
        try: await self.controller.power_state.update(PowerStateStatus(True))
        except (ConnectionAbortedError, ConnectionException): pass

    async def async_turn_off(self):
        try: await self.controller.power_state.update(PowerStateStatus(False))
        except (ConnectionAbortedError, ConnectionException): pass

    @property
    def device_info(self):
        dev = self.dev_info or {}
        model = ("BRC1H" + dev["Model Number String"]) if "Model Number String" in dev else ""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "DAIKIN",
            "model": model,
            "sw_version": dev.get("Software Revision String", ""),
            "via_device": (DOMAIN, self.unique_id),
        }