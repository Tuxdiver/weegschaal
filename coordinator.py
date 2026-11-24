from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_last_service_info,
    async_register_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHAR_BODY,
    CHAR_COMMAND,
    CHAR_PERSON,
    CHAR_WEIGHT,
    DOMAIN,
    NUM_USERS,
    TIME_OFFSET,
)

_LOGGER = logging.getLogger(__name__)

# How often we look at HA's advertisement history for the scale. HA only
# dispatches bluetooth callbacks when the advertisement *content* changes, and
# the scale always sends the same advertisement, so after the first weighing
# the callback never fires again. The advertisement time in the history is
# updated on every packet though, so we poll that.
_POLL_INTERVAL = timedelta(seconds=2)
# Advertisements older than this are considered stale (e.g. restored history
# after a HA restart) and never trigger a connection.
_MAX_ADV_AGE = 10.0
# How long to wait after the last BLE packet before giving up
_IDLE_TIMEOUT = 15.0
# Hard cap for the entire session
_SESSION_TIMEOUT = 60.0
# Ignore advertisements for this long after a successful session (the scale
# keeps advertising for a while after it disconnects)
_SESSION_COOLDOWN = 30.0
# Backoff after failed connection attempts (doubles per failure, capped)
_RETRY_BACKOFF = 5.0
_RETRY_BACKOFF_MAX = 60.0


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

_LB_TO_KG = 0.45359237


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
    if len(data) < 14 or data[0] not in (0x1D, 0x3D):
        return False, 0, 0.0, 0
    weight_kg  = struct.unpack_from("<H", data, 1)[0] / 100.0
    if data[0] == 0x3D:
        # scale is set to lb
        weight_kg *= _LB_TO_KG
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
        self._unsub: list[Callable[[], None]] = []
        self._session_task: asyncio.Task | None = None
        # Monotonic time of the last advertisement we have looked at
        self._last_adv_time: float = 0.0
        # Advertisements before this monotonic time are ignored
        self._ignore_until: float = 0.0
        self._failures = 0
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

        # Change-driven callback: fires on the first advertisement after the
        # scale was unavailable and whenever the advertisement content changes.
        self._unsub.append(
            async_register_callback(
                self.hass,
                self._on_ble_advertisement,
                {"address": self.address, "connectable": True},
                BluetoothScanningMode.ACTIVE,
            )
        )
        # Poll the advertisement history to catch repeated identical
        # advertisements, which HA does not dispatch to callbacks.
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_poll, _POLL_INTERVAL)
        )
        _LOGGER.debug("Watching BLE advertisements of %s", self.address)

    async def async_stop(self) -> None:
        while self._unsub:
            self._unsub.pop()()
        if self._session_task and not self._session_task.done():
            self._session_task.cancel()
            try:
                await self._session_task
            except asyncio.CancelledError:
                pass
        self._session_task = None

    @callback
    def _on_ble_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        self._handle_advertisement(service_info)

    @callback
    def _async_poll(self, _now: datetime) -> None:
        service_info = async_last_service_info(self.hass, self.address, connectable=True)
        if service_info is not None:
            self._handle_advertisement(service_info)

    @callback
    def _handle_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        adv_time = service_info.time
        if adv_time <= self._last_adv_time:
            return  # already seen
        self._last_adv_time = adv_time

        now = time.monotonic()
        age = now - adv_time
        _LOGGER.debug(
            "BLE advertisement: connectable=%s age=%.1fs rssi=%s busy=%s ignore=%.1fs",
            service_info.connectable,
            age,
            service_info.rssi,
            self._session_task is not None,
            max(0.0, self._ignore_until - now),
        )

        if self._session_task is not None:
            return
        if not service_info.connectable:
            return
        if age > _MAX_ADV_AGE:
            # e.g. history restored after a restart; the scale is not awake
            return
        if adv_time < self._ignore_until:
            return

        # The scale only advertises while it is awake (right after someone
        # stepped on it), so any fresh connectable advertisement means
        # "connect now" - just like the ESPHome ble_client does.
        self._session_task = self.hass.async_create_background_task(
            self._async_run_session(service_info.device),
            f"{DOMAIN}_{self.address}_session",
        )

    async def _async_run_session(self, device: BLEDevice) -> None:
        connected = False
        try:
            connected = await self._async_do_session(device)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("BLE session with %s failed", self.address)
        finally:
            # After a real session the scale keeps advertising for a while;
            # don't reconnect for that. After a failed attempt retry soon.
            if connected:
                self._failures = 0
                delay = _SESSION_COOLDOWN
            else:
                delay = min(_RETRY_BACKOFF * 2 ** self._failures, _RETRY_BACKOFF_MAX)
                self._failures += 1
            self._ignore_until = time.monotonic() + delay
            self._session_task = None

    async def _async_do_session(self, device: BLEDevice) -> bool:
        """Run one BLE session. Returns True if a connection was established."""
        _LOGGER.debug("Starting session with %s", self.address)

        # Per-session buffers — keep only the newest entry per person
        person_data:  dict[int, tuple] = {}   # pid -> (male, age, size_cm, high_act)
        weight_data:  dict[int, tuple] = {}   # pid -> (weight_kg, timestamp)
        body_data:    dict[int, tuple] = {}   # pid -> (timestamp, kcal, fat, tbw, muscle, bone)

        loop = asyncio.get_running_loop()
        last_packet: list[float] = [loop.time()]
        done = asyncio.Event()

        def _touch() -> None:
            last_packet[0] = loop.time()

        def on_person(_, data: bytearray) -> None:
            ok, pid, male, age, size_cm, high_act = _decode_person(bytes(data))
            if ok and 1 <= pid <= NUM_USERS:
                person_data[pid] = (male, age, size_cm, high_act)
                _LOGGER.debug("Person %d: male=%s age=%d size=%dcm", pid, male, age, size_cm)
            _touch()

        def on_weight(_, data: bytearray) -> None:
            ok, pid, weight_kg, ts = _decode_weight(bytes(data), self.use_timeoffset)
            if ok and 1 <= pid <= NUM_USERS and ts <= int(time.time()):
                existing = weight_data.get(pid)
                if existing is None or ts > existing[1]:
                    weight_data[pid] = (weight_kg, ts)
                    _LOGGER.debug("Weight %d: %.2f kg @ %d", pid, weight_kg, ts)
            _touch()

        def on_body(_, data: bytearray) -> None:
            ok, pid, ts, kcal, fat, tbw, muscle, bone = _decode_body(bytes(data), self.use_timeoffset)
            if ok and 1 <= pid <= NUM_USERS and ts <= int(time.time()):
                existing = body_data.get(pid)
                if existing is None or ts > existing[0]:
                    body_data[pid] = (ts, kcal, fat, tbw, muscle, bone)
                    _LOGGER.debug("Body %d: kcal=%d fat=%.1f%%", pid, kcal, fat)
            _touch()

        def _fresh_device() -> BLEDevice:
            return (
                async_ble_device_from_address(self.hass, self.address, connectable=True)
                or device
            )

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                f"Medisana BS444 ({self.address})",
                disconnected_callback=lambda _: done.set(),
                max_attempts=3,
                ble_device_callback=_fresh_device,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not connect to scale: %s", exc)
            return False

        try:
            if done.is_set() or not client.is_connected:
                _LOGGER.warning("Scale disconnected before GATT setup (proxy too slow?)")
                return False

            await client.start_notify(CHAR_PERSON, on_person)
            await client.start_notify(CHAR_WEIGHT, on_weight)
            await client.start_notify(CHAR_BODY, on_body)

            ts_cmd = int(time.time())
            if self.use_timeoffset:
                ts_cmd -= TIME_OFFSET
            cmd = bytes([0x02]) + struct.pack("<I", ts_cmd)
            await client.write_gatt_char(CHAR_COMMAND, cmd, response=True)
            _touch()  # start idle timer only after command is sent
            _LOGGER.debug("Command sent (ts=%d), waiting for data…", ts_cmd)

            # Wait until the scale disconnects itself, or we time out
            deadline = loop.time() + _SESSION_TIMEOUT
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                if loop.time() - last_packet[0] > _IDLE_TIMEOUT:
                    _LOGGER.debug("No data for %.0fs, closing session", _IDLE_TIMEOUT)
                    break
                if loop.time() > deadline:
                    _LOGGER.warning("Session timeout after %.0fs", _SESSION_TIMEOUT)
                    break
        finally:
            # Always release the proxy connection slot, even if the scale
            # already dropped the link or the session was cancelled.
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Error while disconnecting: %s", exc)

        self._publish(person_data, weight_data, body_data)
        await self._store.async_save(
            {str(uid): asdict(meas) for uid, meas in self._measurements.items()}
        )
        return True

    @callback
    def _publish(
        self,
        person_data: dict[int, tuple],
        weight_data: dict[int, tuple],
        body_data: dict[int, tuple],
    ) -> None:
        if not (person_data or weight_data or body_data):
            _LOGGER.debug("Session finished without data")
            return

        for pid in sorted(set(person_data) | set(weight_data) | set(body_data)):
            old = self._measurements.get(pid, UserMeasurement())
            meas = UserMeasurement(**asdict(old))

            if pid in person_data:
                male, age, size_cm, high_act = person_data[pid]
                meas.age           = age or None
                meas.size_cm       = size_cm or None
                meas.male          = male
                meas.high_activity = high_act
            if pid in weight_data:
                w_kg, w_ts = weight_data[pid]
                meas.weight    = round(w_kg, 2)
                meas.timestamp = w_ts
                if meas.size_cm:
                    size_m = meas.size_cm / 100.0
                    meas.bmi = round(w_kg / (size_m ** 2), 1)
            if pid in body_data:
                b_ts, kcal, fat, tbw, muscle, bone = body_data[pid]
                meas.kcal   = kcal
                meas.fat    = fat
                meas.tbw    = tbw
                meas.muscle = muscle
                meas.bone   = bone
                if pid not in weight_data:
                    meas.timestamp = b_ts

            self._measurements[pid] = meas
            _LOGGER.info("Measurement user %d: weight=%.2f kg", pid, meas.weight or 0.0)

        self.async_set_updated_data(dict(self._measurements))

    def get_measurement(self, user_id: int) -> UserMeasurement:
        return self._measurements.get(user_id, UserMeasurement())
