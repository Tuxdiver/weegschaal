from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHAR_BODY,
    CHAR_COMMAND,
    CHAR_PERSON,
    CHAR_WEIGHT,
    DOMAIN,
    NUM_USERS,
    SERVICE_UUID_ACTIVE,
    TIME_OFFSET,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait after the last BLE packet before giving up
_IDLE_TIMEOUT = 15.0
# Hard cap for the entire session
_SESSION_TIMEOUT = 60.0
# Ignore BLE advertisements for this long after a session ends (prevents
# spurious re-triggers from the post-disconnect re-advertisement burst)
_SESSION_COOLDOWN = 30.0


@dataclass
class UserMeasurement:
    # Person profile
    age: int | None = None
    size_cm: int | None = None
    male: bool | None = None
    high_activity: bool | None = None
    # Weight
    weight: float | None = None
    bmi: float | None = None
    timestamp: int | None = None
    # Body composition
    kcal: int | None = None
    fat: float | None = None
    tbw: float | None = None
    muscle: float | None = None
    bone: float | None = None


# ---------------------------------------------------------------------------
# Packet decoders (ported 1-to-1 from Scale.cpp)
# ---------------------------------------------------------------------------

def _sanitize_ts(raw: int, use_offset: bool) -> int:
    if use_offset:
        result = raw + TIME_OFFSET
        # guard against overflow beyond max int32
        if result > 0x7FFFFFFF:
            result = raw
    else:
        result = raw
    if raw >= 0x7FFFFFFF:
        result = 0
    return result


def _decode_person(data: bytes) -> tuple[bool, int, bool, int, int, bool]:
    """Return (valid, person_id, male, age, size_cm, high_activity)."""
    if len(data) < 9 or data[0] != 0x84:
        return False, 0, True, 0, 0, False
    person_id   = data[2]
    male        = data[4] == 1
    age         = data[5]
    size_cm     = data[6]        # raw byte is already cm (e.g. 175)
    high_act    = data[8] == 3
    return True, person_id, male, age, size_cm, high_act


def _decode_weight(data: bytes, use_offset: bool) -> tuple[bool, int, float, int]:
    """Return (valid, person_id, weight_kg, timestamp)."""
    if len(data) < 14 or data[0] != 0x1D:
        return False, 0, 0.0, 0
    weight_kg  = struct.unpack_from("<H", data, 1)[0] / 100.0
    raw_ts     = struct.unpack_from("<I", data, 5)[0]
    timestamp  = _sanitize_ts(raw_ts, use_offset)
    person_id  = data[13]
    return True, person_id, weight_kg, timestamp


def _decode_body(data: bytes, use_offset: bool) -> tuple[bool, int, int, int, float, float, float, float]:
    """Return (valid, person_id, timestamp, kcal, fat, tbw, muscle, bone)."""
    if len(data) < 16 or data[0] != 0x6F:
        return False, 0, 0, 0, 0.0, 0.0, 0.0, 0.0
    raw_ts    = struct.unpack_from("<I", data, 1)[0]
    timestamp = _sanitize_ts(raw_ts, use_offset)
    person_id = data[5]
    kcal      = struct.unpack_from("<H", data, 6)[0]
    fat       = (struct.unpack_from("<H", data, 8)[0]  & 0x0FFF) / 10.0
    tbw       = (struct.unpack_from("<H", data, 10)[0] & 0x0FFF) / 10.0
    muscle    = (struct.unpack_from("<H", data, 12)[0] & 0x0FFF) / 10.0
    bone      = (struct.unpack_from("<H", data, 14)[0] & 0x0FFF) / 10.0
    return True, person_id, timestamp, kcal, fat, tbw, muscle, bone


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class MedisanaCoordinator(DataUpdateCoordinator[dict[int, UserMeasurement]]):
    def __init__(self, hass: HomeAssistant, address: str, use_timeoffset: bool) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.address        = address.upper()
        self.use_timeoffset = use_timeoffset
        self._measurements: dict[int, UserMeasurement] = {
            uid: UserMeasurement() for uid in range(1, NUM_USERS + 1)
        }
        self._cancel_callback: Callable | None = None
        self._connecting = False
        self._last_session_end: float = -_SESSION_COOLDOWN  # allow immediate first trigger
        self._store = Store(hass, 1, f"{DOMAIN}.{address.replace(':', '_')}")

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored:
            for uid_str, data in stored.items():
                uid = int(uid_str)
                if uid in self._measurements:
                    self._measurements[uid] = UserMeasurement(**data)
            self.async_set_updated_data(dict(self._measurements))
            _LOGGER.debug("Restored measurements from storage")

        self._cancel_callback = async_register_callback(
            self.hass,
            self._on_ble_advertisement,
            {"address": self.address},
            BluetoothScanningMode.ACTIVE,
        )
        _LOGGER.debug("Registered BLE callback for %s", self.address)

    async def async_stop(self) -> None:
        if self._cancel_callback:
            self._cancel_callback()
            self._cancel_callback = None

    @callback
    def _on_ble_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        now = time.monotonic()
        cooldown_remaining = _SESSION_COOLDOWN - (now - self._last_session_end)

        is_active = (
            SERVICE_UUID_ACTIVE in service_info.service_uuids
            or bool(service_info.manufacturer_data)
        )

        _LOGGER.debug(
            "BLE advertisement: connectable=%s active=%s connecting=%s cooldown=%.1fs",
            service_info.connectable,
            is_active,
            self._connecting,
            max(0.0, cooldown_remaining),
        )

        if self._connecting:
            return
        if cooldown_remaining > 0:
            if is_active and service_info.connectable:
                # Active advertisement seen during cooldown — schedule a deferred
                # connect attempt so we don't miss the weighing session if HA
                # doesn't re-deliver this advertisement after the cooldown expires.
                self.hass.async_create_task(
                    self._async_deferred_session(service_info.device, cooldown_remaining)
                )
            return
        if not service_info.connectable:
            return
        # Only connect when scale is active — standby advertisements have only
        # one service UUID and no manufacturer data; active weighing mode has both.
        if not is_active:
            _LOGGER.debug("Scale in standby mode, skipping")
            return
        self._connecting = True
        self.hass.async_create_task(self._async_run_session(service_info.device))

    async def _async_deferred_session(self, device, delay: float) -> None:
        """Wait out the cooldown, then attempt a session with the cached device.

        This handles the case where an active-mode advertisement arrives while the
        cooldown is still active.  HA may not re-deliver the same advertisement
        once the cooldown expires, so we schedule the connect ourselves.
        """
        _LOGGER.debug("Deferred session: waiting %.1fs for cooldown", delay)
        await asyncio.sleep(delay + 0.5)  # small extra margin
        if self._connecting:
            _LOGGER.debug("Deferred session: already connecting, bailing")
            return
        now = time.monotonic()
        if now - self._last_session_end < _SESSION_COOLDOWN:
            _LOGGER.debug("Deferred session: cooldown still active after sleep, bailing")
            return
        # Prefer a fresh device handle if the proxy has seen a newer advertisement
        fresh = async_ble_device_from_address(self.hass, self.address, connectable=True)
        target = fresh or device
        if target is None:
            _LOGGER.debug("Deferred session: no connectable device found")
            return
        _LOGGER.debug("Deferred session: starting BLE session")
        self._connecting = True
        await self._async_run_session(target)

    async def _async_run_session(self, device) -> None:
        try:
            connected = await self._async_do_session(device)
            if connected:
                # Only apply cooldown after a real connection — prevents spurious
                # re-triggers from the post-disconnect advertisement burst.
                # After a failed connect attempt there is no such burst, so we
                # must NOT block the next advertisement.
                self._last_session_end = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("BLE session failed: %s", exc)
        finally:
            self._connecting = False

    async def _async_do_session(self, device) -> bool:
        """Run one BLE session. Returns True if a connection was established."""
        _LOGGER.debug("Starting session with %s", self.address)

        # Per-session buffers — keep only the newest entry per person
        person_data:  dict[int, tuple] = {}   # pid -> (male, age, size_cm, high_act)
        weight_data:  dict[int, tuple] = {}   # pid -> (weight_kg, timestamp)
        body_data:    dict[int, tuple] = {}   # pid -> (timestamp, kcal, fat, tbw, muscle, bone)

        now_ts = asyncio.get_event_loop().time
        last_packet: list[float] = [now_ts()]
        done = asyncio.Event()

        def _touch() -> None:
            last_packet[0] = now_ts()

        def on_person(_, data: bytearray) -> None:
            ok, pid, male, age, size_cm, high_act = _decode_person(bytes(data))
            if ok and 1 <= pid <= NUM_USERS:
                person_data[pid] = (male, age, size_cm, high_act)
                _LOGGER.debug("Person %d: male=%s age=%d size=%dcm", pid, male, age, size_cm)
            _touch()

        def on_weight(_, data: bytearray) -> None:
            ok, pid, weight_kg, ts = _decode_weight(bytes(data), self.use_timeoffset)
            if ok and 1 <= pid <= NUM_USERS and ts <= int(datetime.now().timestamp()):
                existing = weight_data.get(pid)
                if existing is None or ts > existing[1]:
                    weight_data[pid] = (weight_kg, ts)
                    _LOGGER.debug("Weight %d: %.2f kg @ %d", pid, weight_kg, ts)
            _touch()

        def on_body(_, data: bytearray) -> None:
            ok, pid, ts, kcal, fat, tbw, muscle, bone = _decode_body(bytes(data), self.use_timeoffset)
            if ok and 1 <= pid <= NUM_USERS and ts <= int(datetime.now().timestamp()):
                existing = body_data.get(pid)
                if existing is None or ts > existing[0]:
                    body_data[pid] = (ts, kcal, fat, tbw, muscle, bone)
                    _LOGGER.debug("Body %d: kcal=%d fat=%.1f%%", pid, kcal, fat)
            _touch()

        try:
            client = await establish_connection(
                BleakClient,
                device,
                self.address,
                disconnected_callback=lambda _: done.set(),
                max_attempts=3,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not connect to scale: %s", exc)
            return False

        try:
            await client.start_notify(CHAR_PERSON, on_person)
            await client.start_notify(CHAR_WEIGHT, on_weight)
            await client.start_notify(CHAR_BODY, on_body)

            ts_cmd = int(datetime.now().timestamp())
            if self.use_timeoffset:
                ts_cmd -= TIME_OFFSET
            cmd = bytes([0x02]) + struct.pack("<I", ts_cmd)
            await client.write_gatt_char(CHAR_COMMAND, cmd, response=True)
            last_packet[0] = now_ts()  # start idle timer only after command is sent
            _LOGGER.debug("Command sent (ts=%d), waiting for data…", ts_cmd)

            # Wait until the scale disconnects itself, or we time out
            deadline = now_ts() + _SESSION_TIMEOUT
            while not done.is_set():
                await asyncio.sleep(1.0)
                if now_ts() - last_packet[0] > _IDLE_TIMEOUT:
                    _LOGGER.debug("No data for %.0fs, closing session", _IDLE_TIMEOUT)
                    break
                if now_ts() > deadline:
                    _LOGGER.warning("Session timeout after %.0fs", _SESSION_TIMEOUT)
                    break
        finally:
            if client.is_connected:
                await client.disconnect()

        # Publish measurements
        for pid, (male, age, size_cm, high_act) in person_data.items():
            meas = UserMeasurement(
                age=age or None,
                size_cm=size_cm or None,
                male=male,
                high_activity=high_act,
            )
            if pid in weight_data:
                w_kg, w_ts = weight_data[pid]
                meas.weight    = round(w_kg, 2)
                meas.timestamp = w_ts
                if size_cm:
                    size_m = size_cm / 100.0
                    meas.bmi = round(w_kg / (size_m ** 2), 1)
            if pid in body_data:
                b_ts, kcal, fat, tbw, muscle, bone = body_data[pid]
                meas.kcal   = kcal
                meas.fat    = fat
                meas.tbw    = tbw
                meas.muscle = muscle
                meas.bone   = bone
                if meas.timestamp is None:
                    meas.timestamp = b_ts

            self._measurements[pid] = meas
            _LOGGER.info("Measurement user %d: weight=%.2f kg", pid, meas.weight or 0.0)

        self.async_set_updated_data(dict(self._measurements))
        await self._store.async_save(
            {str(uid): asdict(meas) for uid, meas in self._measurements.items()}
        )
        return True

    def get_measurement(self, user_id: int) -> UserMeasurement:
        return self._measurements.get(user_id, UserMeasurement())
