"""Support for August binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from yalexs.activity import (
    ACTION_DOORBELL_CALL_MISSED,
    SOURCE_PUBNUB,
    Activity,
    ActivityType,
)
from yalexs.doorbell import Doorbell, DoorbellDetail
from yalexs.lock import Lock, LockDetail, LockDoorStatus
from yalexs.manager.const import ACTIVITY_UPDATE_INTERVAL
from yalexs.util import update_lock_detail_from_activity

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import AugustConfigEntry, AugustData
from .entity import AugustEntityMixin

_LOGGER = logging.getLogger(__name__)

TIME_TO_DECLARE_DETECTION = timedelta(seconds=ACTIVITY_UPDATE_INTERVAL.total_seconds())
TIME_TO_RECHECK_DETECTION = timedelta(
    seconds=ACTIVITY_UPDATE_INTERVAL.total_seconds() * 3
)


def _retrieve_online_state(
    data: AugustData, detail: DoorbellDetail | LockDetail
) -> bool:
    """Get the latest state of the sensor."""
    # The doorbell will go into standby mode when there is no motion
    # for a short while. It will wake by itself when needed so we need
    # to consider is available or we will not report motion or dings
    if isinstance(detail, DoorbellDetail):
        return detail.is_online or detail.is_standby
    return detail.bridge_is_online


def _retrieve_motion_state(data: AugustData, detail: DoorbellDetail) -> bool:
    assert data.activity_stream is not None
    latest = data.activity_stream.get_latest_device_activity(
        detail.device_id, {ActivityType.DOORBELL_MOTION}
    )

    if latest is None:
        return False

    return _activity_time_based_state(latest)


def _retrieve_image_capture_state(data: AugustData, detail: DoorbellDetail) -> bool:
    assert data.activity_stream is not None
    latest = data.activity_stream.get_latest_device_activity(
        detail.device_id, {ActivityType.DOORBELL_IMAGE_CAPTURE}
    )

    if latest is None:
        return False

    return _activity_time_based_state(latest)


def _retrieve_ding_state(data: AugustData, detail: DoorbellDetail | LockDetail) -> bool:
    assert data.activity_stream is not None
    latest = data.activity_stream.get_latest_device_activity(
        detail.device_id, {ActivityType.DOORBELL_DING}
    )

    if latest is None:
        return False

    if (
        data.activity_stream.pubnub.connected
        and latest.action == ACTION_DOORBELL_CALL_MISSED
    ):
        return False

    return _activity_time_based_state(latest)


def _activity_time_based_state(latest: Activity) -> bool:
    """Get the latest state of the sensor."""
    start = latest.activity_start_time
    end = latest.activity_end_time + TIME_TO_DECLARE_DETECTION
    return start <= _native_datetime() <= end


def _native_datetime() -> datetime:
    """Return time in the format august uses without timezone."""
    return datetime.now()


@dataclass(frozen=True, kw_only=True)
class AugustDoorbellBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes August binary_sensor entity."""

    value_fn: Callable[[AugustData, DoorbellDetail], bool]
    is_time_based: bool


SENSOR_TYPE_DOOR = BinarySensorEntityDescription(
    key="open",
    device_class=BinarySensorDeviceClass.DOOR,
)

SENSOR_TYPES_VIDEO_DOORBELL = (
    AugustDoorbellBinarySensorEntityDescription(
        key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=_retrieve_motion_state,
        is_time_based=True,
    ),
    AugustDoorbellBinarySensorEntityDescription(
        key="image capture",
        translation_key="image_capture",
        value_fn=_retrieve_image_capture_state,
        is_time_based=True,
    ),
    AugustDoorbellBinarySensorEntityDescription(
        key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_retrieve_online_state,
        is_time_based=False,
    ),
)


SENSOR_TYPES_DOORBELL: tuple[AugustDoorbellBinarySensorEntityDescription, ...] = (
    AugustDoorbellBinarySensorEntityDescription(
        key="ding",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=_retrieve_ding_state,
        is_time_based=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AugustConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the August binary sensors."""
    data = config_entry.runtime_data
    entities: list[BinarySensorEntity] = []

    for lock in data.locks:
        detail = data.get_device_detail(lock.device_id)
        if detail.doorsense:
            entities.append(AugustDoorBinarySensor(data, lock, SENSOR_TYPE_DOOR))

        if detail.doorbell:
            entities.extend(
                AugustDoorbellBinarySensor(data, lock, description)
                for description in SENSOR_TYPES_DOORBELL
            )

    for doorbell in data.doorbells:
        entities.extend(
            AugustDoorbellBinarySensor(data, doorbell, description)
            for description in SENSOR_TYPES_DOORBELL + SENSOR_TYPES_VIDEO_DOORBELL
        )

    async_add_entities(entities)


class AugustDoorBinarySensor(AugustEntityMixin, BinarySensorEntity):
    """Representation of an August Door binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(
        self,
        data: AugustData,
        device: Lock,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(data, device)
        self.entity_description = description
        self._attr_unique_id = f"{self._device_id}_{description.key}"

    @callback
    def _update_from_data(self) -> None:
        """Get the latest state of the sensor and update activity."""
        assert self._data.activity_stream is not None
        door_activity = self._data.activity_stream.get_latest_device_activity(
            self._device_id, {ActivityType.DOOR_OPERATION}
        )

        if door_activity is not None:
            update_lock_detail_from_activity(self._detail, door_activity)
            # If the source is pubnub the lock must be online since its a live update
            if door_activity.source == SOURCE_PUBNUB:
                self._detail.set_online(True)

        bridge_activity = self._data.activity_stream.get_latest_device_activity(
            self._device_id, {ActivityType.BRIDGE_OPERATION}
        )

        if bridge_activity is not None:
            update_lock_detail_from_activity(self._detail, bridge_activity)
        self._attr_available = self._detail.bridge_is_online
        self._attr_is_on = self._detail.door_state == LockDoorStatus.OPEN

    async def async_added_to_hass(self) -> None:
        """Set the initial state when adding to hass."""
        self._update_from_data()
        await super().async_added_to_hass()


class AugustDoorbellBinarySensor(AugustEntityMixin, BinarySensorEntity):
    """Representation of an August binary sensor."""

    entity_description: AugustDoorbellBinarySensorEntityDescription

    def __init__(
        self,
        data: AugustData,
        device: Doorbell | Lock,
        description: AugustDoorbellBinarySensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(data, device)
        self.entity_description = description
        self._check_for_off_update_listener: Callable[[], None] | None = None
        self._data = data
        self._attr_unique_id = f"{self._device_id}_{description.key}"

    @callback
    def _update_from_data(self) -> None:
        """Get the latest state of the sensor."""
        self._cancel_any_pending_updates()
        self._attr_is_on = self.entity_description.value_fn(self._data, self._detail)

        if self.entity_description.is_time_based:
            self._attr_available = _retrieve_online_state(self._data, self._detail)
            self._schedule_update_to_recheck_turn_off_sensor()
        else:
            self._attr_available = True

    @callback
    def _async_scheduled_update(self, now: datetime) -> None:
        """Timer callback for sensor update."""
        self._check_for_off_update_listener = None
        self._update_from_data()
        if not self.is_on:
            self.async_write_ha_state()

    def _schedule_update_to_recheck_turn_off_sensor(self) -> None:
        """Schedule an update to recheck the sensor to see if it is ready to turn off."""
        # If the sensor is already off there is nothing to do
        if not self.is_on:
            return
        self._check_for_off_update_listener = async_call_later(
            self.hass, TIME_TO_RECHECK_DETECTION, self._async_scheduled_update
        )

    def _cancel_any_pending_updates(self) -> None:
        """Cancel any updates to recheck a sensor to see if it is ready to turn off."""
        if not self._check_for_off_update_listener:
            return
        _LOGGER.debug("%s: canceled pending update", self.entity_id)
        self._check_for_off_update_listener()
        self._check_for_off_update_listener = None

    async def async_added_to_hass(self) -> None:
        """Call the mixin to subscribe and setup an async_track_point_in_utc_time to turn off the sensor if needed."""
        self._update_from_data()
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """When removing cancel any scheduled updates."""
        self._cancel_any_pending_updates()
        await super().async_will_remove_from_hass()
