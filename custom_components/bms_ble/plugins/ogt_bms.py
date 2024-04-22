"""Module to support Offgridtec Smart Pro BMS."""
import asyncio
import logging
from typing import Any

from bleak import BleakClient, normalize_uuid_str
from bleak.backends.device import BLEDevice

from ..const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    ATTR_CYCLES,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
)
from .basebms import BaseBMS

LOGGER = logging.getLogger(__name__)
BAT_TIMEOUT = 1


class OGTBms(BaseBMS):
    """Offgridtec LiFePO4 Smart Pro type A and type B battery class implementation."""

    # magic crypt sequence of length 16
    _CRYPT_SEQ = [2, 5, 4, 3, 1, 4, 1, 6, 8, 3, 7, 2, 5, 8, 9, 3]
    # setup UUIDs, e.g. for receive: '0000fff4-0000-1000-8000-00805f9b34fb'
    UUID_RX = normalize_uuid_str("FFF4")
    UUID_TX = normalize_uuid_str("FFF6")
    UUID_SERVICE = normalize_uuid_str("FFF0")

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        self._reconnect = reconnect
        self._ble_device = ble_device
        assert self._ble_device.name is not None
        self._client: BleakClient | None = None
        self._data_event = asyncio.Event()
        self._connected = False  # flag to indicate active BLE connection
        self._type = self._ble_device.name[9]
        self._key = sum(
            self._CRYPT_SEQ[int(c, 16)]
            for c in (f"{int(self._ble_device.name[10:]):0>4X}")
        ) + (5 if (self._type == "A") else 8)
        LOGGER.info(
            "%s type: %c, ID: %s, key: %x",
            self.device_id(),
            self._type,
            self._ble_device.name[10:],
            self._key,
        )
        self._values: dict[str, float] = {}  # dictionary of queried values

        if self._type == "A":
            self._OGT_REGISTERS = {
                # SOC (State of Charge)
                2: {"name": ATTR_BATTERY_LEVEL, "len": 1, "func": lambda x: int(x)},
                4: {
                    "name": ATTR_CYCLE_CHRG,
                    "len": 3,
                    "func": lambda x: float(x) / 1000,
                },
                8: {"name": ATTR_VOLTAGE, "len": 2, "func": lambda x: float(x) / 1000},
                # MOS temperature
                12: {
                    "name": ATTR_TEMPERATURE,
                    "len": 2,
                    "func": lambda x: round(float(x) * 0.1 - 273.15, 1),
                },
                # length for current is actually only 2, 3 used to detect signed value
                16: {"name": ATTR_CURRENT, "len": 3, "func": lambda x: float(x) / 100},
                24: {"name": ATTR_RUNTIME, "len": 2, "func": lambda x: int(x * 60)},
                44: {"name": ATTR_CYCLES, "len": 2, "func": lambda x: int(x)},
            }
            self._OGT_HEADER = "+RAA"
        elif self._type == "B":
            self._OGT_REGISTERS = {
                # MOS temperature
                8: {
                    "name": ATTR_TEMPERATURE,
                    "len": 2,
                    "func": lambda x: round(float(x) * 0.1 - 273.15, 1),
                },
                9: {"name": ATTR_VOLTAGE, "len": 2, "func": lambda x: float(x) / 1000},
                10: {"name": ATTR_CURRENT, "len": 3, "func": lambda x: float(x) / 1000},
                # SOC (State of Charge)
                13: {"name": ATTR_BATTERY_LEVEL, "len": 1, "func": lambda x: int(x)},
                15: {
                    "name": ATTR_CYCLE_CHRG,
                    "len": 3,
                    "func": lambda x: float(x) / 1000,
                },
                18: {"name": ATTR_RUNTIME, "len": 2, "func": lambda x: int(x * 60)},
                23: {"name": ATTR_CYCLES, "len": 2, "func": lambda x: int(x)},
            }
            self._OGT_HEADER = "+R16"
        else:
            self._OGT_REGISTERS = {}
            LOGGER.exception("Unkown device type '%c'", self._type)

    @staticmethod
    def matcher_dict_list() -> list[dict[str, Any]]:
        """Return a list of Bluetooth matchers."""
        return [
            {"local_name": "SmartBat-A*", "connectable": True},
            {"local_name": "SmartBat-B*", "connectable": True},
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return a dictionary of device information."""
        return {"manufacturer": "Offgridtec", "model": "LiFePo4 Smart Pro"}

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    async def async_update(self) -> dict[str, int | float | bool]:
        """Update battery status information."""

        await self._connect()

        self._values.clear()
        for key in list(self._OGT_REGISTERS):
            await self._read(key)
            try:
                await asyncio.wait_for(self._wait_event(), timeout=BAT_TIMEOUT)
            except TimeoutError:
                LOGGER.debug("Reading %s timed out.", self._OGT_REGISTERS[key]["name"])

        self.calc_values(
            self._values, {ATTR_CYCLE_CAP, ATTR_POWER, ATTR_BATTERY_CHARGING}
        )

        LOGGER.debug("Data collected: %s", self._values)
        if self._reconnect:
            # disconnect after data update to force reconnect next time (slow!)
            await self.disconnect()
        return self._values

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback for Bleak."""

        LOGGER.debug("Disconnected from %s.", client.address)
        self._connected = False

    def _notification_handler(self, sender, data: bytearray) -> None:
        LOGGER.debug("Received BLE data: %s", data)

        valid, reg, nat_value = self._ogt_response(data)

        # check that descambled message is valid and from the right characteristic
        if valid and sender.uuid == self.UUID_RX:
            register = self._OGT_REGISTERS[reg]
            value = register["func"](nat_value)
            LOGGER.debug(
                "Decoded data: reg: %s (#%i), raw: %i, value: %f",
                register["name"],
                reg,
                nat_value,
                value,
            )
            self._values[register["name"]] = value
        else:
            LOGGER.debug("Response is invalid.")
        self._data_event.set()

    async def _connect(self) -> None:
        """Connect to the BMS and setup notification if not connected."""

        if not self._connected:
            LOGGER.debug("Connecting BMS (%s).", self._ble_device.name)
            self._client = BleakClient(
                self._ble_device.address,
                disconnected_callback=self._on_disconnect,
                services=[self.UUID_SERVICE],
            )
            await self._client.connect()
            await self._client.start_notify(self.UUID_RX, self._notification_handler)
            self._connected = True
        else:
            LOGGER.debug("BMS %s already connected", self._ble_device.name)

    async def disconnect(self) -> None:
        """Disconnect the BMS, includes stoping notifications."""

        if self._client and self._connected:
            LOGGER.debug("Disconnecting BMS (%s)", self._ble_device.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except Exception:
                LOGGER.warning("disconnect failed!")

        self._client = None

    def _ogt_response(self, resp: bytearray) -> tuple:
        """Descramble a response from the BMS."""

        msg = bytearray((resp[x] ^ self._key) for x in range(0, len(resp))).decode(
            encoding="ascii"
        )
        LOGGER.debug("response: %s", msg[:-2])
        # verify correct response
        if msg[:4] != "+RD," or msg[-2:] != "\r\n":
            return False, None, None
        # 16-bit value in network order (plus optional multiplier for 24-bit values)
        signed = len(msg) > 12
        value = int.from_bytes(
            bytes.fromhex(msg[6:10]), byteorder="little", signed=signed
        ) * (max(int(msg[10:12], 16), 1) if signed else 1)
        return True, int(msg[4:6], 16), value

    def _ogt_command(self, command: int) -> bytes:
        """Put together an scambled query to the BMS."""

        cmd = f"{self._OGT_HEADER}{command:0>2X}{self._OGT_REGISTERS[command]['len']:0>2X}"
        LOGGER.debug("command: %s", cmd)

        return bytearray(ord(cmd[i]) ^ self._key for i in range(len(cmd)))

    async def _read(self, reg: int) -> None:
        """Read a specific BMS register."""
        assert self._client is not None

        msg = self._ogt_command(reg)
        LOGGER.debug("BLE cmd frame %s", msg)
        await self._client.write_gatt_char(self.UUID_TX, data=msg)
