"""esphome session fixtures."""
from __future__ import annotations

from asyncio import Event
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from aioesphomeapi import (
    APIClient,
    APIVersion,
    DeviceInfo,
    EntityInfo,
    EntityState,
    ReconnectLogic,
    UserService,
)
import pytest
from zeroconf import Zeroconf

from homeassistant.components.esphome import (
    CONF_DEVICE_NAME,
    CONF_NOISE_PSK,
    DOMAIN,
    dashboard,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from . import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_SLUG

from tests.common import MockConfigEntry


@pytest.fixture(autouse=True)
def mock_bluetooth(enable_bluetooth):
    """Auto mock bluetooth."""


@pytest.fixture(autouse=True)
def esphome_mock_async_zeroconf(mock_async_zeroconf):
    """Auto mock zeroconf."""


@pytest.fixture(autouse=True)
async def load_homeassistant(hass) -> None:
    """Load the homeassistant integration."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def mock_config_entry(hass) -> MockConfigEntry:
    """Return the default mocked config entry."""
    config_entry = MockConfigEntry(
        title="ESPHome Device",
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.2",
            CONF_PORT: 6053,
            CONF_PASSWORD: "pwd",
            CONF_NOISE_PSK: "12345678123456781234567812345678",
            CONF_DEVICE_NAME: "test",
        },
        unique_id="11:22:33:44:55:aa",
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def mock_device_info() -> DeviceInfo:
    """Return the default mocked device info."""
    return DeviceInfo(
        uses_password=False,
        name="test",
        legacy_bluetooth_proxy_version=0,
        mac_address="11:22:33:44:55:aa",
        esphome_version="1.0.0",
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the ESPHome integration for testing."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    return mock_config_entry


@pytest.fixture
def mock_client(mock_device_info) -> APIClient:
    """Mock APIClient."""
    mock_client = Mock(spec=APIClient)

    def mock_constructor(
        address: str,
        port: int,
        password: str | None,
        *,
        client_info: str = "aioesphomeapi",
        keepalive: float = 15.0,
        zeroconf_instance: Zeroconf = None,
        noise_psk: str | None = None,
        expected_name: str | None = None,
    ):
        """Fake the client constructor."""
        mock_client.host = address
        mock_client.port = port
        mock_client.password = password
        mock_client.zeroconf_instance = zeroconf_instance
        mock_client.noise_psk = noise_psk
        return mock_client

    mock_client.side_effect = mock_constructor
    mock_client.device_info = AsyncMock(return_value=mock_device_info)
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.list_entities_services = AsyncMock(return_value=([], []))
    mock_client.api_version = APIVersion(99, 99)

    with patch("homeassistant.components.esphome.APIClient", mock_client), patch(
        "homeassistant.components.esphome.config_flow.APIClient", mock_client
    ):
        yield mock_client


@pytest.fixture
async def mock_dashboard(hass):
    """Mock dashboard."""
    data = {"configured": [], "importable": []}
    with patch(
        "esphome_dashboard_api.ESPHomeDashboardAPI.get_devices",
        return_value=data,
    ):
        await dashboard.async_set_dashboard_info(
            hass, DASHBOARD_SLUG, DASHBOARD_HOST, DASHBOARD_PORT
        )
        yield data


async def _mock_generic_device_entry(
    hass: HomeAssistant,
    mock_client: APIClient,
    mock_device_info: dict[str, Any],
    mock_list_entities_services: tuple[list[EntityInfo], list[UserService]],
    states: list[EntityState],
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "test.local",
            CONF_PORT: 6053,
            CONF_PASSWORD: "",
        },
    )
    entry.add_to_hass(hass)

    device_info = DeviceInfo(
        name="test",
        friendly_name="Test",
        mac_address="11:22:33:44:55:aa",
        esphome_version="1.0.0",
        **mock_device_info,
    )

    async def _subscribe_states(callback: Callable[[EntityState], None]) -> None:
        """Subscribe to state."""
        for state in states:
            callback(state)

    mock_client.device_info = AsyncMock(return_value=device_info)
    mock_client.subscribe_voice_assistant = AsyncMock(return_value=Mock())
    mock_client.list_entities_services = AsyncMock(
        return_value=mock_list_entities_services
    )
    mock_client.subscribe_states = _subscribe_states

    try_connect_done = Event()
    real_try_connect = ReconnectLogic._try_connect

    async def mock_try_connect(self):
        """Set an event when ReconnectLogic._try_connect has been awaited."""
        result = await real_try_connect(self)
        try_connect_done.set()
        return result

    with patch.object(ReconnectLogic, "_try_connect", mock_try_connect):
        await hass.config_entries.async_setup(entry.entry_id)
        await try_connect_done.wait()

    await hass.async_block_till_done()

    return entry


@pytest.fixture
async def mock_voice_assistant_entry(
    hass: HomeAssistant,
    mock_client: APIClient,
):
    """Set up an ESPHome entry with voice assistant."""

    async def _mock_voice_assistant_entry(version: int) -> MockConfigEntry:
        return await _mock_generic_device_entry(
            hass, mock_client, {"voice_assistant_version": version}, ([], []), []
        )

    return _mock_voice_assistant_entry


@pytest.fixture
async def mock_voice_assistant_v1_entry(mock_voice_assistant_entry) -> MockConfigEntry:
    """Set up an ESPHome entry with voice assistant."""
    return await mock_voice_assistant_entry(version=1)


@pytest.fixture
async def mock_voice_assistant_v2_entry(mock_voice_assistant_entry) -> MockConfigEntry:
    """Set up an ESPHome entry with voice assistant."""
    return await mock_voice_assistant_entry(version=2)


@pytest.fixture
async def mock_generic_device_entry(
    hass: HomeAssistant,
) -> MockConfigEntry:
    """Set up an ESPHome entry."""

    async def _mock_device_entry(
        mock_client: APIClient,
        entity_info: list[EntityInfo],
        user_service: list[UserService],
        states: list[EntityState],
    ) -> MockConfigEntry:
        return await _mock_generic_device_entry(
            hass, mock_client, {}, (entity_info, user_service), states
        )

    return _mock_device_entry
