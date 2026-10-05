"""Tests for Overkiz integration init."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError
from pyoverkiz.exceptions import (
    MaintenanceError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from pyoverkiz.models import Device
import pytest

from homeassistant import config_entries
from homeassistant.components.overkiz.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import MockOverkizClient
from .test_config_flow import TEST_EMAIL, TEST_GATEWAY_ID, TEST_PASSWORD, TEST_SERVER

from tests.common import (
    MockConfigEntry,
    RegistryEntryWithDefaults,
    async_fire_time_changed,
    mock_registry,
)

SETUP_FIXTURE_WITH_INTERNAL_DEVICES = "setup/local_somfy_tahoma_switch_europe.json"
ENTITY_COVER_LIVING_ROOM_BLINDS = "cover.living_room_blinds"
PAST_FIRST_SETUP_RETRY = timedelta(seconds=30)
ENTITY_SENSOR_DISCRETE_RSSI_LEVEL = "sensor.zipscreen_woonkamer_discrete_rssi_level"
ENTITY_ALARM_CONTROL_PANEL = "alarm_control_panel.alarm"
ENTITY_SWITCH_GARAGE = "switch.garage"
ENTITY_SENSOR_TARGET_CLOSURE_STATE = "sensor.zipscreen_woonkamer_target_closure_state"
ENTITY_SENSOR_TARGET_CLOSURE_STATE_2 = (
    "sensor.zipscreen_woonkamer_target_closure_state_2"
)


async def test_unique_id_migration(hass: HomeAssistant) -> None:
    """Test migration of sensor unique IDs."""

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_GATEWAY_ID,
        data={"username": TEST_EMAIL, "password": TEST_PASSWORD, "hub": TEST_SERVER},
        minor_version=1,
    )

    mock_entry.add_to_hass(hass)

    mock_registry(
        hass,
        {
            # This entity will be migrated to "io://1234-5678-1234/3541212-core:DiscreteRSSILevelState"
            ENTITY_SENSOR_DISCRETE_RSSI_LEVEL: RegistryEntryWithDefaults(
                entity_id=ENTITY_SENSOR_DISCRETE_RSSI_LEVEL,
                unique_id="io://1234-5678-1234/3541212-OverkizState.CORE_DISCRETE_RSSI_LEVEL",
                platform=DOMAIN,
                config_entry_id=mock_entry.entry_id,
            ),
            # This entity will be migrated to "internal://1234-5678-1234/alarm/0-TSKAlarmController"
            ENTITY_ALARM_CONTROL_PANEL: RegistryEntryWithDefaults(
                entity_id=ENTITY_ALARM_CONTROL_PANEL,
                unique_id="internal://1234-5678-1234/alarm/0-UIWidget.TSKALARM_CONTROLLER",
                platform=DOMAIN,
                config_entry_id=mock_entry.entry_id,
            ),
            # This entity will be migrated to "io://1234-5678-1234/0-OnOff"
            ENTITY_SWITCH_GARAGE: RegistryEntryWithDefaults(
                entity_id=ENTITY_SWITCH_GARAGE,
                unique_id="io://1234-5678-1234/0-UIClass.ON_OFF",
                platform=DOMAIN,
                config_entry_id=mock_entry.entry_id,
            ),
            # This entity will be removed since
            # "io://1234-5678-1234/3541212-core:TargetClosureState"
            # already exists
            ENTITY_SENSOR_TARGET_CLOSURE_STATE: RegistryEntryWithDefaults(
                entity_id=ENTITY_SENSOR_TARGET_CLOSURE_STATE,
                unique_id="io://1234-5678-1234/3541212-OverkizState.CORE_TARGET_CLOSURE",
                platform=DOMAIN,
                config_entry_id=mock_entry.entry_id,
            ),
            # This entity will not be migrated"
            ENTITY_SENSOR_TARGET_CLOSURE_STATE_2: RegistryEntryWithDefaults(
                entity_id=ENTITY_SENSOR_TARGET_CLOSURE_STATE_2,
                unique_id="io://1234-5678-1234/3541212-core:TargetClosureState",
                platform=DOMAIN,
                config_entry_id=mock_entry.entry_id,
            ),
        },
    )
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)  # pylint: disable=home-assistant-tests-registry-fixtures

    unique_id_map = {
        ENTITY_SENSOR_DISCRETE_RSSI_LEVEL: "io://1234-5678-1234/3541212-core:DiscreteRSSILevelState",
        ENTITY_ALARM_CONTROL_PANEL: "internal://1234-5678-1234/alarm/0-TSKAlarmController",
        ENTITY_SWITCH_GARAGE: "io://1234-5678-1234/0-OnOff",
        ENTITY_SENSOR_TARGET_CLOSURE_STATE_2: "io://1234-5678-1234/3541212-core:TargetClosureState",
    }

    # Test if entities will be removed
    assert set(ent_reg.entities.keys()) == set(unique_id_map)

    # Test if unique ids are migrated
    for entity_id, unique_id in unique_id_map.items():
        entry = ent_reg.async_get(entity_id)
        assert entry.unique_id == unique_id

    # Test if the config entry is migrated to the latest minor version
    assert mock_entry.minor_version == 2


async def test_setup_rexel_local_uses_local_client(
    hass: HomeAssistant,
    mock_rexel_local_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
) -> None:
    """A Rexel gateway configured via the Local API must not use OAuth2."""
    mock_rexel_local_config_entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.overkiz.create_local_client",
            return_value=mock_client,
        ) as mock_create_local_client,
        patch(
            "homeassistant.components.overkiz.create_rexel_client"
        ) as mock_create_rexel_client,
    ):
        await hass.config_entries.async_setup(mock_rexel_local_config_entry.entry_id)
        await hass.async_block_till_done()

    mock_create_local_client.assert_called_once_with(
        hass,
        host="gateway-1234-5678-9123.local:8443",
        token="1234123412341234",
        verify_ssl=True,
    )
    mock_create_rexel_client.assert_not_called()
    assert mock_rexel_local_config_entry.state is ConfigEntryState.LOADED


async def test_setup_token_reauth_error_starts_reauth(
    hass: HomeAssistant, mock_rexel_config_entry: MockConfigEntry
) -> None:
    """A non-recoverable token refresh failure triggers reauth."""
    mock_rexel_config_entry.add_to_hass(hass)

    client = AsyncMock()
    client.login.side_effect = OAuth2TokenRequestReauthError(
        request_info=MagicMock(), domain=DOMAIN
    )

    with patch(
        "homeassistant.components.overkiz.create_rexel_client", return_value=client
    ):
        await hass.config_entries.async_setup(mock_rexel_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_rexel_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == config_entries.SOURCE_REAUTH


@pytest.mark.parametrize(
    "exception",
    [
        OAuth2TokenRequestError(request_info=MagicMock(), domain=DOMAIN),
        TooManyRequestsError("Too many requests"),
        MaintenanceError("Server is down for maintenance"),
        ServiceUnavailableError("Server is unavailable"),
        TimeoutError("Timed out"),
        ClientError("Connection error"),
    ],
    ids=[
        "oauth2_token_request",
        "too_many_requests",
        "maintenance",
        "service_unavailable",
        "timeout",
        "client_error",
    ],
)
async def test_setup_transient_error_retries(
    hass: HomeAssistant,
    mock_rexel_config_entry: MockConfigEntry,
    exception: Exception,
) -> None:
    """A recoverable error during setup retries instead of failing."""
    mock_rexel_config_entry.add_to_hass(hass)

    client = AsyncMock()
    client.login.side_effect = exception

    with patch(
        "homeassistant.components.overkiz.create_rexel_client", return_value=client
    ):
        await hass.config_entries.async_setup(mock_rexel_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_rexel_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


NO_REAL_DEVICES = [
    pytest.param(False, id="no_devices"),
    pytest.param(True, id="internal_only"),
]


def _internal_devices(client: MockOverkizClient) -> list[Device]:
    """Return the gateway's own devices from the loaded setup fixture."""
    internal = [
        device
        for device in client.setup.devices
        if device.device_url.startswith("internal://")
    ]
    assert internal
    assert len(internal) < len(client.setup.devices)
    return internal


def _report_no_real_devices(client: MockOverkizClient, keep_internal: bool) -> None:
    """Make the setup list no devices, or only the gateway's internal ones."""
    client.setup.devices = _internal_devices(client) if keep_internal else []


@pytest.mark.parametrize("keep_internal", NO_REAL_DEVICES)
async def test_setup_local_without_real_devices_retries(
    hass: HomeAssistant,
    mock_local_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
    keep_internal: bool,
) -> None:
    """A local gateway reporting no real devices yet is retried, not left dead."""
    mock_local_config_entry.add_to_hass(hass)
    mock_client.set_setup_fixture(SETUP_FIXTURE_WITH_INTERNAL_DEVICES)
    _report_no_real_devices(mock_client, keep_internal)

    with patch(
        "homeassistant.components.overkiz.create_local_client",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(mock_local_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_local_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_local_config_entry.reason == "Gateway has not reported its devices yet"


@pytest.mark.parametrize("keep_internal", NO_REAL_DEVICES)
async def test_setup_local_without_real_devices_does_not_poll_events(
    hass: HomeAssistant,
    mock_local_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
    keep_internal: bool,
) -> None:
    """The not-ready guard runs before the first event refresh."""
    mock_local_config_entry.add_to_hass(hass)
    mock_client.set_setup_fixture(SETUP_FIXTURE_WITH_INTERNAL_DEVICES)
    _report_no_real_devices(mock_client, keep_internal)

    with patch(
        "homeassistant.components.overkiz.create_local_client",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(mock_local_config_entry.entry_id)
        await hass.async_block_till_done()

    mock_client.fetch_events.assert_not_awaited()


@pytest.mark.parametrize(
    "real_device_url",
    [
        pytest.param("zigbee://1234-5678-6508/65535", id="protocol_gateway_stack"),
        pytest.param("io://1234-5678-6508/6380765", id="blind"),
    ],
)
@pytest.mark.parametrize("real_device_first", [True, False], ids=["first", "last"])
async def test_setup_local_with_one_real_device_loads(
    hass: HomeAssistant,
    mock_local_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
    real_device_url: str,
    real_device_first: bool,
) -> None:
    """A single non-internal device is enough, wherever it is listed."""
    mock_local_config_entry.add_to_hass(hass)
    mock_client.set_setup_fixture(SETUP_FIXTURE_WITH_INTERNAL_DEVICES)
    real_device = next(
        device
        for device in mock_client.setup.devices
        if device.device_url == real_device_url
    )
    internal_devices = _internal_devices(mock_client)
    mock_client.setup.devices = (
        [real_device, *internal_devices]
        if real_device_first
        else [*internal_devices, real_device]
    )

    with patch(
        "homeassistant.components.overkiz.create_local_client",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(mock_local_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_local_config_entry.state is ConfigEntryState.LOADED


async def test_setup_local_loads_once_gateway_reports_devices(
    hass: HomeAssistant,
    mock_local_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
) -> None:
    """A retried local setup loads once the gateway lists its real devices."""
    mock_local_config_entry.add_to_hass(hass)
    mock_client.set_setup_fixture(SETUP_FIXTURE_WITH_INTERNAL_DEVICES)
    all_devices = mock_client.setup.devices
    _report_no_real_devices(mock_client, keep_internal=True)

    with patch(
        "homeassistant.components.overkiz.create_local_client",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(mock_local_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_local_config_entry.state is ConfigEntryState.SETUP_RETRY

        mock_client.setup.devices = all_devices
        async_fire_time_changed(hass, dt_util.utcnow() + PAST_FIRST_SETUP_RETRY)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_local_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY_COVER_LIVING_ROOM_BLINDS) is not None


@pytest.mark.parametrize("keep_internal", NO_REAL_DEVICES)
async def test_setup_cloud_without_real_devices_loads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MockOverkizClient,
    keep_internal: bool,
) -> None:
    """The no-real-devices retry only applies to the local API."""
    mock_config_entry.add_to_hass(hass)
    mock_client.set_setup_fixture(SETUP_FIXTURE_WITH_INTERNAL_DEVICES)
    _report_no_real_devices(mock_client, keep_internal)

    with patch(
        "homeassistant.components.overkiz.create_cloud_client",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
