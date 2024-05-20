"""Test the Daly BMS implementation."""

from typing import Union

from bleak import BleakError, BleakGATTCharacteristic, normalize_uuid_str
from typing_extensions import Buffer

from custom_components.bms_ble.plugins.daly_bms import BMS

from .conftest import MockBleakClient

from .bluetooth import generate_ble_device


class MockDalyBleakClient(MockBleakClient):
    """Emulate a Daly BMS BleakClient."""

    HEAD_READ = bytearray(b"\xD2\x03")
    CMD_INFO = bytearray(b"\x00\x00\x00\x3E\xD7\xB9")

    def _response(
        self, char_specifier: Union[BleakGATTCharacteristic, int, str], data: Buffer
    ) -> bytearray:
        if char_specifier == normalize_uuid_str("fff2") and data == (
            self.HEAD_READ + self.CMD_INFO
        ):
            return bytearray(
                b"\xd2\x03|\x10\x1f\x10)\x103\x10=\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00<\x00=\x00>\x00?\x00\x00\x00\x00\x00\x00\x00\x00\x00\x8cuN\x03\x84\x10=\x10\x1f\x00\x00\x00\x00\x00\x00\r\x80\x00\x04\x00\x04\x009\x00\x01\x00\x00\x00\x01\x10.\x0f\xa0\x00*\x00\x00\x00\x00\x00\x00\x00\x00\x1a7"
            )
        # {'voltage': 14.0, 'current': 3.0, 'battery_level': 90.0, 'cycles': 57, 'cycle_charge': 345.6, 'numTemp': 4, 'temperature': 21.5, 'cycle_capacity': 4838.400000000001, 'power': 42.0, 'battery_charging': True, 'runtime': 414720, 'rssi': -78}

        return bytearray()

    async def write_gatt_char(
        self,
        char_specifier: Union[BleakGATTCharacteristic, int, str],
        data: Buffer,
        response: bool = None,  # type: ignore # same as upstream
    ) -> None:
        """Issue write command to GATT."""
        await super().write_gatt_char(char_specifier, data, response)
        assert self._notify_callback is not None
        self._notify_callback(
            "MockDalyBleakClient", self._response(char_specifier, data)
        )


class MockInvalidBleakClient(MockDalyBleakClient):
    """Emulate a Daly BMS BleakClient."""

    def _response(
        self, char_specifier: Union[BleakGATTCharacteristic, int, str], data: Buffer
    ) -> bytearray:
        if char_specifier == normalize_uuid_str("fff2"):
            return bytearray(b"invalid_value")

        return bytearray()

    async def disconnect(self) -> bool:
        """Mock disconnect to raise BleakError."""
        raise BleakError


async def test_update(monkeypatch, reconnect_fixture) -> None:
    """Test Daly BMS data update."""

    monkeypatch.setattr(
        "custom_components.bms_ble.plugins.daly_bms.BleakClient",
        MockDalyBleakClient,
    )

    bms = BMS(
        generate_ble_device("cc:cc:cc:cc:cc:cc", "MockBLEdevice", None, -73),
        reconnect_fixture,
    )

    result = await bms.async_update()

    assert result == {
        "voltage": 14.0,
        "current": 3.0,
        "battery_level": 90.0,
        "cycles": 57,
        "cycle_charge": 345.6,
        "numTemp": 4,
        "temperature": 21.5,
        "cycle_capacity": 4838.400000000001,
        "power": 42.0,
        "battery_charging": True,
        "runtime": 414720,
    }

    # query again to check already connected state
    result = await bms.async_update()
    assert bms._connected is not reconnect_fixture

    await bms.disconnect()


async def test_invalid_response(monkeypatch) -> None:
    """Test data update with BMS returning invalid data."""

    monkeypatch.setattr(
        "custom_components.bms_ble.plugins.daly_bms.BleakClient",
        MockInvalidBleakClient,
    )

    bms = BMS(generate_ble_device("cc:cc:cc:cc:cc:cc", "MockBLEdevice", None, -73))

    result = await bms.async_update()

    assert result == {}

    await bms.disconnect()
