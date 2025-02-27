"""Support for esphome devices."""
from __future__ import annotations

from collections.abc import Callable
import functools
import logging
import math
from typing import Any, Generic, NamedTuple, TypeVar, cast

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    APIVersion,
    DeviceInfo as EsphomeDeviceInfo,
    EntityCategory as EsphomeEntityCategory,
    EntityInfo,
    EntityState,
    HomeassistantServiceCall,
    InvalidAuthAPIError,
    InvalidEncryptionKeyAPIError,
    ReconnectLogic,
    RequiresEncryptionAPIError,
    UserService,
    UserServiceArgType,
    VoiceAssistantEventType,
)
from awesomeversion import AwesomeVersion
import voluptuous as vol

from homeassistant.components import tag, zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_HOST,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
    EntityCategory,
    __version__ as ha_version,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import template
import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.template import Template
from homeassistant.helpers.typing import ConfigType

from .bluetooth import async_connect_scanner
from .const import DOMAIN
from .dashboard import async_get_dashboard, async_setup as async_setup_dashboard
from .domain_data import DomainData

# Import config flow so that it's added to the registry
from .entry_data import RuntimeEntryData
from .enum_mapper import EsphomeEnumMapper
from .voice_assistant import VoiceAssistantUDPServer

CONF_DEVICE_NAME = "device_name"
CONF_NOISE_PSK = "noise_psk"
_LOGGER = logging.getLogger(__name__)
_R = TypeVar("_R")

STABLE_BLE_VERSION_STR = "2023.6.0"
STABLE_BLE_VERSION = AwesomeVersion(STABLE_BLE_VERSION_STR)
PROJECT_URLS = {
    "esphome.bluetooth-proxy": "https://esphome.github.io/bluetooth-proxies/",
}
DEFAULT_URL = f"https://esphome.io/changelog/{STABLE_BLE_VERSION_STR}.html"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@callback
def _async_check_firmware_version(
    hass: HomeAssistant, device_info: EsphomeDeviceInfo, api_version: APIVersion
) -> None:
    """Create or delete an the ble_firmware_outdated issue."""
    # ESPHome device_info.mac_address is the unique_id
    issue = f"ble_firmware_outdated-{device_info.mac_address}"
    if (
        not device_info.bluetooth_proxy_feature_flags_compat(api_version)
        # If the device has a project name its up to that project
        # to tell them about the firmware version update so we don't notify here
        or (device_info.project_name and device_info.project_name not in PROJECT_URLS)
        or AwesomeVersion(device_info.esphome_version) >= STABLE_BLE_VERSION
    ):
        async_delete_issue(hass, DOMAIN, issue)
        return
    async_create_issue(
        hass,
        DOMAIN,
        issue,
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        learn_more_url=PROJECT_URLS.get(device_info.project_name, DEFAULT_URL),
        translation_key="ble_firmware_outdated",
        translation_placeholders={
            "name": device_info.name,
            "version": STABLE_BLE_VERSION_STR,
        },
    )


@callback
def _async_check_using_api_password(
    hass: HomeAssistant, device_info: EsphomeDeviceInfo, has_password: bool
) -> None:
    """Create or delete an the api_password_deprecated issue."""
    # ESPHome device_info.mac_address is the unique_id
    issue = f"api_password_deprecated-{device_info.mac_address}"
    if not has_password:
        async_delete_issue(hass, DOMAIN, issue)
        return
    async_create_issue(
        hass,
        DOMAIN,
        issue,
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        learn_more_url="https://esphome.io/components/api.html",
        translation_key="api_password_deprecated",
        translation_placeholders={
            "name": device_info.name,
        },
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the esphome component."""
    await async_setup_dashboard(hass)
    return True


async def async_setup_entry(  # noqa: C901
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up the esphome component."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    password = entry.data[CONF_PASSWORD]
    noise_psk = entry.data.get(CONF_NOISE_PSK)
    device_id: str = None  # type: ignore[assignment]

    zeroconf_instance = await zeroconf.async_get_instance(hass)

    cli = APIClient(
        host,
        port,
        password,
        client_info=f"Home Assistant {ha_version}",
        zeroconf_instance=zeroconf_instance,
        noise_psk=noise_psk,
    )

    domain_data = DomainData.get(hass)
    entry_data = RuntimeEntryData(
        client=cli,
        entry_id=entry.entry_id,
        store=domain_data.get_or_create_store(hass, entry),
    )
    domain_data.set_entry_data(entry, entry_data)

    async def on_stop(event: Event) -> None:
        """Cleanup the socket client on HA stop."""
        await _cleanup_instance(hass, entry)

    # Use async_listen instead of async_listen_once so that we don't deregister
    # the callback twice when shutting down Home Assistant.
    # "Unable to remove unknown listener
    # <function EventBus.async_listen_once.<locals>.onetime_listener>"
    entry_data.cleanup_callbacks.append(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, on_stop)
    )

    @callback
    def async_on_service_call(service: HomeassistantServiceCall) -> None:
        """Call service when user automation in ESPHome config is triggered."""
        domain, service_name = service.service.split(".", 1)
        service_data = service.data

        if service.data_template:
            try:
                data_template = {
                    key: Template(value) for key, value in service.data_template.items()
                }
                template.attach(hass, data_template)
                service_data.update(
                    template.render_complex(data_template, service.variables)
                )
            except TemplateError as ex:
                _LOGGER.error("Error rendering data template for %s: %s", host, ex)
                return

        if service.is_event:
            # ESPHome uses servicecall packet for both events and service calls
            # Ensure the user can only send events of form 'esphome.xyz'
            if domain != "esphome":
                _LOGGER.error(
                    "Can only generate events under esphome domain! (%s)", host
                )
                return

            # Call native tag scan
            if service_name == "tag_scanned" and device_id is not None:
                tag_id = service_data["tag_id"]
                hass.async_create_task(tag.async_scan_tag(hass, tag_id, device_id))
                return

            hass.bus.async_fire(
                service.service,
                {
                    ATTR_DEVICE_ID: device_id,
                    **service_data,
                },
            )
        else:
            hass.async_create_task(
                hass.services.async_call(
                    domain, service_name, service_data, blocking=True
                )
            )

    async def _send_home_assistant_state(
        entity_id: str, attribute: str | None, state: State | None
    ) -> None:
        """Forward Home Assistant states to ESPHome."""
        if state is None or (attribute and attribute not in state.attributes):
            return

        send_state = state.state
        if attribute:
            attr_val = state.attributes[attribute]
            # ESPHome only handles "on"/"off" for boolean values
            if isinstance(attr_val, bool):
                send_state = "on" if attr_val else "off"
            else:
                send_state = attr_val

        await cli.send_home_assistant_state(entity_id, attribute, str(send_state))

    @callback
    def async_on_state_subscription(
        entity_id: str, attribute: str | None = None
    ) -> None:
        """Subscribe and forward states for requested entities."""

        async def send_home_assistant_state_event(event: Event) -> None:
            """Forward Home Assistant states updates to ESPHome."""

            # Only communicate changes to the state or attribute tracked
            if event.data.get("new_state") is None or (
                event.data.get("old_state") is not None
                and "new_state" in event.data
                and (
                    (
                        not attribute
                        and event.data["old_state"].state
                        == event.data["new_state"].state
                    )
                    or (
                        attribute
                        and attribute in event.data["old_state"].attributes
                        and attribute in event.data["new_state"].attributes
                        and event.data["old_state"].attributes[attribute]
                        == event.data["new_state"].attributes[attribute]
                    )
                )
            ):
                return

            await _send_home_assistant_state(
                event.data["entity_id"], attribute, event.data.get("new_state")
            )

        unsub = async_track_state_change_event(
            hass, [entity_id], send_home_assistant_state_event
        )
        entry_data.disconnect_callbacks.append(unsub)

        # Send initial state
        hass.async_create_task(
            _send_home_assistant_state(entity_id, attribute, hass.states.get(entity_id))
        )

    voice_assistant_udp_server: VoiceAssistantUDPServer | None = None

    def _handle_pipeline_event(
        event_type: VoiceAssistantEventType, data: dict[str, str] | None
    ) -> None:
        cli.send_voice_assistant_event(event_type, data)

    def _handle_pipeline_finished() -> None:
        nonlocal voice_assistant_udp_server

        entry_data.async_set_assist_pipeline_state(False)

        if voice_assistant_udp_server is not None:
            voice_assistant_udp_server.close()
            voice_assistant_udp_server = None

    async def _handle_pipeline_start(conversation_id: str, use_vad: bool) -> int | None:
        """Start a voice assistant pipeline."""
        nonlocal voice_assistant_udp_server

        if voice_assistant_udp_server is not None:
            return None

        voice_assistant_udp_server = VoiceAssistantUDPServer(
            hass, entry_data, _handle_pipeline_event, _handle_pipeline_finished
        )
        port = await voice_assistant_udp_server.start_server()

        hass.async_create_background_task(
            voice_assistant_udp_server.run_pipeline(
                device_id=device_id,
                conversation_id=conversation_id or None,
                use_vad=use_vad,
            ),
            "esphome.voice_assistant_udp_server.run_pipeline",
        )
        entry_data.async_set_assist_pipeline_state(True)

        return port

    async def _handle_pipeline_stop() -> None:
        """Stop a voice assistant pipeline."""
        nonlocal voice_assistant_udp_server

        if voice_assistant_udp_server is not None:
            voice_assistant_udp_server.stop()

    async def on_connect() -> None:
        """Subscribe to states and list entities on successful API login."""
        nonlocal device_id
        try:
            device_info = await cli.device_info()

            # Migrate config entry to new unique ID if necessary
            # This was changed in 2023.1
            if entry.unique_id != format_mac(device_info.mac_address):
                hass.config_entries.async_update_entry(
                    entry, unique_id=format_mac(device_info.mac_address)
                )

            # Make sure we have the correct device name stored
            # so we can map the device to ESPHome Dashboard config
            if entry.data.get(CONF_DEVICE_NAME) != device_info.name:
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_DEVICE_NAME: device_info.name}
                )

            entry_data.device_info = device_info
            assert cli.api_version is not None
            entry_data.api_version = cli.api_version
            entry_data.available = True
            if entry_data.device_info.name:
                reconnect_logic.name = entry_data.device_info.name

            if device_info.bluetooth_proxy_feature_flags_compat(cli.api_version):
                entry_data.disconnect_callbacks.append(
                    await async_connect_scanner(hass, entry, cli, entry_data)
                )

            device_id = _async_setup_device_registry(
                hass, entry, entry_data.device_info
            )
            entry_data.async_update_device_state(hass)

            entity_infos, services = await cli.list_entities_services()
            await entry_data.async_update_static_infos(hass, entry, entity_infos)
            await _setup_services(hass, entry_data, services)
            await cli.subscribe_states(entry_data.async_update_state)
            await cli.subscribe_service_calls(async_on_service_call)
            await cli.subscribe_home_assistant_states(async_on_state_subscription)

            if device_info.voice_assistant_version:
                entry_data.disconnect_callbacks.append(
                    await cli.subscribe_voice_assistant(
                        _handle_pipeline_start,
                        _handle_pipeline_stop,
                    )
                )

            hass.async_create_task(entry_data.async_save_to_store())
        except APIConnectionError as err:
            _LOGGER.warning("Error getting initial data for %s: %s", host, err)
            # Re-connection logic will trigger after this
            await cli.disconnect()
        else:
            _async_check_firmware_version(hass, device_info, entry_data.api_version)
            _async_check_using_api_password(hass, device_info, bool(password))

    async def on_disconnect() -> None:
        """Run disconnect callbacks on API disconnect."""
        name = entry_data.device_info.name if entry_data.device_info else host
        _LOGGER.debug("%s: %s disconnected, running disconnected callbacks", name, host)
        for disconnect_cb in entry_data.disconnect_callbacks:
            disconnect_cb()
        entry_data.disconnect_callbacks = []
        entry_data.available = False
        # Mark state as stale so that we will always dispatch
        # the next state update of that type when the device reconnects
        entry_data.stale_state = {
            (type(entity_state), key)
            for state_dict in entry_data.state.values()
            for key, entity_state in state_dict.items()
        }
        if not hass.is_stopping:
            # Avoid marking every esphome entity as unavailable on shutdown
            # since it generates a lot of state changed events and database
            # writes when we already know we're shutting down and the state
            # will be cleared anyway.
            entry_data.async_update_device_state(hass)

    async def on_connect_error(err: Exception) -> None:
        """Start reauth flow if appropriate connect error type."""
        if isinstance(
            err,
            (
                RequiresEncryptionAPIError,
                InvalidEncryptionKeyAPIError,
                InvalidAuthAPIError,
            ),
        ):
            entry.async_start_reauth(hass)

    reconnect_logic = ReconnectLogic(
        client=cli,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        zeroconf_instance=zeroconf_instance,
        name=host,
        on_connect_error=on_connect_error,
    )

    infos, services = await entry_data.async_load_from_store()
    await entry_data.async_update_static_infos(hass, entry, infos)
    await _setup_services(hass, entry_data, services)

    if entry_data.device_info is not None and entry_data.device_info.name:
        reconnect_logic.name = entry_data.device_info.name
        if entry.unique_id is None:
            hass.config_entries.async_update_entry(
                entry, unique_id=format_mac(entry_data.device_info.mac_address)
            )

    await reconnect_logic.start()
    entry_data.cleanup_callbacks.append(reconnect_logic.stop_callback)

    return True


@callback
def _async_setup_device_registry(
    hass: HomeAssistant, entry: ConfigEntry, device_info: EsphomeDeviceInfo
) -> str:
    """Set up device registry feature for a particular config entry."""
    sw_version = device_info.esphome_version
    if device_info.compilation_time:
        sw_version += f" ({device_info.compilation_time})"

    configuration_url = None
    if device_info.webserver_port > 0:
        configuration_url = f"http://{entry.data['host']}:{device_info.webserver_port}"
    elif dashboard := async_get_dashboard(hass):
        configuration_url = f"homeassistant://hassio/ingress/{dashboard.addon_slug}"

    manufacturer = "espressif"
    if device_info.manufacturer:
        manufacturer = device_info.manufacturer
    model = device_info.model
    hw_version = None
    if device_info.project_name:
        project_name = device_info.project_name.split(".")
        manufacturer = project_name[0]
        model = project_name[1]
        hw_version = device_info.project_version

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        configuration_url=configuration_url,
        connections={(dr.CONNECTION_NETWORK_MAC, device_info.mac_address)},
        name=device_info.friendly_name or device_info.name,
        manufacturer=manufacturer,
        model=model,
        sw_version=sw_version,
        hw_version=hw_version,
    )
    return device_entry.id


class ServiceMetadata(NamedTuple):
    """Metadata for services."""

    validator: Any
    example: str
    selector: dict[str, Any]
    description: str | None = None


ARG_TYPE_METADATA = {
    UserServiceArgType.BOOL: ServiceMetadata(
        validator=cv.boolean,
        example="False",
        selector={"boolean": None},
    ),
    UserServiceArgType.INT: ServiceMetadata(
        validator=vol.Coerce(int),
        example="42",
        selector={"number": {CONF_MODE: "box"}},
    ),
    UserServiceArgType.FLOAT: ServiceMetadata(
        validator=vol.Coerce(float),
        example="12.3",
        selector={"number": {CONF_MODE: "box", "step": 1e-3}},
    ),
    UserServiceArgType.STRING: ServiceMetadata(
        validator=cv.string,
        example="Example text",
        selector={"text": None},
    ),
    UserServiceArgType.BOOL_ARRAY: ServiceMetadata(
        validator=[cv.boolean],
        description="A list of boolean values.",
        example="[True, False]",
        selector={"object": {}},
    ),
    UserServiceArgType.INT_ARRAY: ServiceMetadata(
        validator=[vol.Coerce(int)],
        description="A list of integer values.",
        example="[42, 34]",
        selector={"object": {}},
    ),
    UserServiceArgType.FLOAT_ARRAY: ServiceMetadata(
        validator=[vol.Coerce(float)],
        description="A list of floating point numbers.",
        example="[ 12.3, 34.5 ]",
        selector={"object": {}},
    ),
    UserServiceArgType.STRING_ARRAY: ServiceMetadata(
        validator=[cv.string],
        description="A list of strings.",
        example="['Example text', 'Another example']",
        selector={"object": {}},
    ),
}


async def _register_service(
    hass: HomeAssistant, entry_data: RuntimeEntryData, service: UserService
) -> None:
    if entry_data.device_info is None:
        raise ValueError("Device Info needs to be fetched first")
    service_name = f"{entry_data.device_info.name.replace('-', '_')}_{service.name}"
    schema = {}
    fields = {}

    for arg in service.args:
        if arg.type not in ARG_TYPE_METADATA:
            _LOGGER.error(
                "Can't register service %s because %s is of unknown type %s",
                service_name,
                arg.name,
                arg.type,
            )
            return
        metadata = ARG_TYPE_METADATA[arg.type]
        schema[vol.Required(arg.name)] = metadata.validator
        fields[arg.name] = {
            "name": arg.name,
            "required": True,
            "description": metadata.description,
            "example": metadata.example,
            "selector": metadata.selector,
        }

    async def execute_service(call: ServiceCall) -> None:
        await entry_data.client.execute_service(service, call.data)

    hass.services.async_register(
        DOMAIN, service_name, execute_service, vol.Schema(schema)
    )

    service_desc = {
        "description": (
            f"Calls the service {service.name} of the node"
            f" {entry_data.device_info.name}"
        ),
        "fields": fields,
    }

    async_set_service_schema(hass, DOMAIN, service_name, service_desc)


async def _setup_services(
    hass: HomeAssistant, entry_data: RuntimeEntryData, services: list[UserService]
) -> None:
    if entry_data.device_info is None:
        # Can happen if device has never connected or .storage cleared
        return
    old_services = entry_data.services.copy()
    to_unregister = []
    to_register = []
    for service in services:
        if service.key in old_services:
            # Already exists
            if (matching := old_services.pop(service.key)) != service:
                # Need to re-register
                to_unregister.append(matching)
                to_register.append(service)
        else:
            # New service
            to_register.append(service)

    for service in old_services.values():
        to_unregister.append(service)

    entry_data.services = {serv.key: serv for serv in services}

    for service in to_unregister:
        service_name = f"{entry_data.device_info.name}_{service.name}"
        hass.services.async_remove(DOMAIN, service_name)

    for service in to_register:
        await _register_service(hass, entry_data, service)


async def _cleanup_instance(
    hass: HomeAssistant, entry: ConfigEntry
) -> RuntimeEntryData:
    """Cleanup the esphome client if it exists."""
    domain_data = DomainData.get(hass)
    data = domain_data.pop_entry_data(entry)
    data.available = False
    for disconnect_cb in data.disconnect_callbacks:
        disconnect_cb()
    data.disconnect_callbacks = []
    for cleanup_callback in data.cleanup_callbacks:
        cleanup_callback()
    await data.client.disconnect()
    return data


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an esphome config entry."""
    entry_data = await _cleanup_instance(hass, entry)
    return await hass.config_entries.async_unload_platforms(
        entry, entry_data.loaded_platforms
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove an esphome config entry."""
    await DomainData.get(hass).get_or_create_store(hass, entry).async_remove()


_InfoT = TypeVar("_InfoT", bound=EntityInfo)
_EntityT = TypeVar("_EntityT", bound="EsphomeEntity[Any,Any]")
_StateT = TypeVar("_StateT", bound=EntityState)


async def platform_async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    *,
    component_key: str,
    info_type: type[_InfoT],
    entity_type: type[_EntityT],
    state_type: type[_StateT],
) -> None:
    """Set up an esphome platform.

    This method is in charge of receiving, distributing and storing
    info and state updates.
    """
    entry_data: RuntimeEntryData = DomainData.get(hass).get_entry_data(entry)
    entry_data.info[component_key] = {}
    entry_data.old_info[component_key] = {}
    entry_data.state.setdefault(state_type, {})

    @callback
    def async_list_entities(infos: list[EntityInfo]) -> None:
        """Update entities of this platform when entities are listed."""
        old_infos = entry_data.info[component_key]
        new_infos: dict[int, EntityInfo] = {}
        add_entities: list[_EntityT] = []
        for info in infos:
            if info.key in old_infos:
                # Update existing entity
                old_infos.pop(info.key)
            else:
                # Create new entity
                entity = entity_type(entry_data, component_key, info, state_type)
                add_entities.append(entity)
            new_infos[info.key] = info

        # Remove old entities
        for info in old_infos.values():
            entry_data.async_remove_entity(hass, component_key, info.key)

        # First copy the now-old info into the backup object
        entry_data.old_info[component_key] = entry_data.info[component_key]
        # Then update the actual info
        entry_data.info[component_key] = new_infos

        for key, new_info in new_infos.items():
            async_dispatcher_send(
                hass,
                entry_data.signal_component_key_static_info_updated(component_key, key),
                new_info,
            )

        if add_entities:
            # Add entities to Home Assistant
            async_add_entities(add_entities)

    entry_data.cleanup_callbacks.append(
        entry_data.async_register_static_info_callback(info_type, async_list_entities)
    )


def esphome_state_property(
    func: Callable[[_EntityT], _R]
) -> Callable[[_EntityT], _R | None]:
    """Wrap a state property of an esphome entity.

    This checks if the state object in the entity is set, and
    prevents writing NAN values to the Home Assistant state machine.
    """

    @functools.wraps(func)
    def _wrapper(self: _EntityT) -> _R | None:
        # pylint: disable-next=protected-access
        if not self._has_state:
            return None
        val = func(self)
        if isinstance(val, float) and math.isnan(val):
            # Home Assistant doesn't use NAN values in state machine
            # (not JSON serializable)
            return None
        return val

    return _wrapper


ICON_SCHEMA = vol.Schema(cv.icon)


ENTITY_CATEGORIES: EsphomeEnumMapper[
    EsphomeEntityCategory, EntityCategory | None
] = EsphomeEnumMapper(
    {
        EsphomeEntityCategory.NONE: None,
        EsphomeEntityCategory.CONFIG: EntityCategory.CONFIG,
        EsphomeEntityCategory.DIAGNOSTIC: EntityCategory.DIAGNOSTIC,
    }
)


class EsphomeEntity(Entity, Generic[_InfoT, _StateT]):
    """Define a base esphome entity."""

    _attr_should_poll = False
    _static_info: _InfoT
    _state: _StateT
    _has_state: bool

    def __init__(
        self,
        entry_data: RuntimeEntryData,
        component_key: str,
        entity_info: EntityInfo,
        state_type: type[_StateT],
    ) -> None:
        """Initialize."""
        self._entry_data = entry_data
        self._on_entry_data_changed()
        self._component_key = component_key
        self._key = entity_info.key
        self._state_type = state_type
        self._on_static_info_update(entity_info)
        assert entry_data.device_info is not None
        device_info = entry_data.device_info
        self._device_info = device_info
        self._attr_has_entity_name = bool(device_info.friendly_name)
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, device_info.mac_address)}
        )
        self._entry_id = entry_data.entry_id

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        entry_data = self._entry_data
        hass = self.hass
        component_key = self._component_key
        key = self._key

        self.async_on_remove(
            async_dispatcher_connect(
                hass,
                f"esphome_{self._entry_id}_remove_{component_key}_{key}",
                functools.partial(self.async_remove, force_remove=True),
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                hass,
                entry_data.signal_device_updated,
                self._on_device_update,
            )
        )
        self.async_on_remove(
            entry_data.async_subscribe_state_update(
                self._state_type, key, self._on_state_update
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                hass,
                entry_data.signal_component_key_static_info_updated(component_key, key),
                self._on_static_info_update,
            )
        )
        self._update_state_from_entry_data()

    @callback
    def _on_static_info_update(self, static_info: EntityInfo) -> None:
        """Save the static info for this entity when it changes.

        This method can be overridden in child classes to know
        when the static info changes.
        """
        static_info = cast(_InfoT, static_info)
        self._static_info = static_info
        self._attr_unique_id = static_info.unique_id
        self._attr_entity_registry_enabled_default = not static_info.disabled_by_default
        self._attr_name = static_info.name
        if entity_category := static_info.entity_category:
            self._attr_entity_category = ENTITY_CATEGORIES.from_esphome(entity_category)
        else:
            self._attr_entity_category = None
        if icon := static_info.icon:
            self._attr_icon = cast(str, ICON_SCHEMA(icon))
        else:
            self._attr_icon = None

    @callback
    def _update_state_from_entry_data(self) -> None:
        """Update state from entry data."""

        state = self._entry_data.state
        key = self._key
        state_type = self._state_type
        has_state = key in state[state_type]
        if has_state:
            self._state = cast(_StateT, state[state_type][key])
        self._has_state = has_state

    @callback
    def _on_state_update(self) -> None:
        """Call when state changed.

        Behavior can be changed in child classes
        """
        self._update_state_from_entry_data()
        self.async_write_ha_state()

    @callback
    def _on_entry_data_changed(self) -> None:
        entry_data = self._entry_data
        self._api_version = entry_data.api_version
        self._client = entry_data.client

    @callback
    def _on_device_update(self) -> None:
        """Call when device updates or entry data changes."""
        self._on_entry_data_changed()
        if not self._entry_data.available:
            # Only write state if the device has gone unavailable
            # since _on_state_update will be called if the device
            # is available when the full state arrives
            # through the next entity state packet.
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        if self._device_info.has_deep_sleep:
            # During deep sleep the ESP will not be connectable (by design)
            # For these cases, show it as available
            return True

        return self._entry_data.available


class EsphomeAssistEntity(Entity):
    """Define a base entity for Assist Pipeline entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_data: RuntimeEntryData) -> None:
        """Initialize the binary sensor."""
        self._entry_data: RuntimeEntryData = entry_data
        assert entry_data.device_info is not None
        device_info = entry_data.device_info
        self._device_info = device_info
        self._attr_unique_id = (
            f"{device_info.mac_address}-{self.entity_description.key}"
        )
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, device_info.mac_address)}
        )

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._entry_data.async_subscribe_assist_pipeline_update(self._update)
        )
