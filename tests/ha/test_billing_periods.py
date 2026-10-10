"""Tests for meters without hourly data, which are read from billing periods."""

from datetime import date

from homeassistant import config_entries
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sew_water.const import (
    CONF_BILLING_ACCOUNT_ID,
    CONF_COOKIES,
    CONF_DIGITAL_METER,
    CONF_METER_ID,
    CONF_METER_SERIAL,
    CONF_STATISTIC_ID,
    CONF_SUPPLY_TYPE,
    DOMAIN,
)
from custom_components.sew_water.sew_client import (
    AccountIds,
    BillingPeriod,
    LoginResult,
    MeterRead,
    SewAuthError,
    SewBusyError,
    SewConnectionError,
    SewProtocolError,
)

from .conftest import BILLING_ACCOUNT_ID, COOKIES, PASSWORD, USERNAME, FakeClient

ANALOGUE_METER_ID = "a1K90000000ANLGEAC"
OTHER_ANALOGUE_METER_ID = "a1K90000000ANL2EAC"

QUARTER = BillingPeriod(
    start=date(2026, 5, 21),
    end=date(2026, 8, 20),
    days=91,
    drinking_kl=16.0,
    drinking_cost=63.25,
    recycled_kl=5.0,
    recycled_cost=11.98,
)
EARLIER_QUARTER = BillingPeriod(
    start=date(2026, 2, 23),
    end=date(2026, 5, 21),
    days=87,
    drinking_kl=15.0,
    drinking_cost=56.53,
    recycled_kl=3.0,
    recycled_cost=7.03,
)
CLOSING_READ = MeterRead(date(2026, 8, 20), 273, 5.0, 90, estimated=False)


def analogue_entry(supply: str = "mains", meter_id: str = ANALOGUE_METER_ID) -> MockConfigEntry:
    """An entry for a meter the portal does not mark as digital."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"{USERNAME} ({supply})",
        unique_id=meter_id,
        version=2,
        data={
            CONF_BILLING_ACCOUNT_ID: BILLING_ACCOUNT_ID,
            CONF_COOKIES: COOKIES,
            CONF_DIGITAL_METER: False,
            CONF_METER_ID: meter_id,
            CONF_METER_SERIAL: "SAHA000001",
            CONF_PASSWORD: PASSWORD,
            CONF_STATISTIC_ID: f"{DOMAIN}:water_usage_{meter_id}",
            CONF_SUPPLY_TYPE: supply,
            CONF_USERNAME: USERNAME,
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def sensor_state(hass: HomeAssistant, entry: MockConfigEntry, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_drinking_water_figures_come_from_the_latest_period(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [EARLIER_QUARTER, QUARTER]
    fake_client.reads = [CLOSING_READ]
    entry = analogue_entry("mains")
    await setup_entry(hass, entry)

    usage = sensor_state(hass, entry, "period_usage")
    assert float(usage.state) == 16.0
    assert usage.attributes["unit_of_measurement"] == "m³"
    assert usage.attributes["period_start"] == "2026-05-21"
    assert usage.attributes["period_end"] == "2026-08-20"
    assert usage.attributes["days"] == 91
    assert usage.attributes["estimated_read"] is False
    assert float(sensor_state(hass, entry, "period_usage_charge").state) == 63.25
    assert float(sensor_state(hass, entry, "period_daily_average").state) == 175.8


async def test_recycled_water_figures_come_from_the_recycled_columns(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.periods = [QUARTER]
    entry = analogue_entry("recycled")
    await setup_entry(hass, entry)

    assert float(sensor_state(hass, entry, "period_usage").state) == 5.0
    assert float(sensor_state(hass, entry, "period_usage_charge").state) == 11.98
    assert float(sensor_state(hass, entry, "period_daily_average").state) == 54.9


async def test_estimated_read_is_reported(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [QUARTER]
    fake_client.reads = [MeterRead(date(2026, 8, 20), 273, 5.0, 90, estimated=True)]
    entry = analogue_entry()
    await setup_entry(hass, entry)
    assert sensor_state(hass, entry, "period_usage").attributes["estimated_read"] is True


async def test_no_hourly_data_is_requested_or_stored(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [QUARTER]
    entry = analogue_entry()
    await setup_entry(hass, entry)

    assert fake_client.fetch_ranges == []
    assert entry.runtime_data.backfill_task is None
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, entry.data[CONF_STATISTIC_ID], True, {"sum"}
    )
    assert rows == {}
    # Only billing sensors exist: no daily or total usage that would suggest measured readings.
    keys = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert f"{entry.entry_id}_daily_usage" not in keys
    assert f"{entry.entry_id}_total_usage" not in keys


async def test_billing_figures_are_not_long_term_statistics(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [QUARTER]
    entry = analogue_entry()
    await setup_entry(hass, entry)
    for key in ("period_usage", "period_daily_average", "period_usage_charge"):
        assert "state_class" not in sensor_state(hass, entry, key).attributes


async def test_repeated_polls_do_not_change_the_figures(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [EARLIER_QUARTER, QUARTER]
    entry = analogue_entry()
    await setup_entry(hass, entry)
    before = {k: sensor_state(hass, entry, k).state for k in ("period_usage", "period_usage_charge")}

    for _ in range(3):
        await entry.runtime_data.async_poll_now()
    await hass.async_block_till_done()

    assert {k: sensor_state(hass, entry, k).state for k in before} == before
    assert fake_client.fetch_ranges == []


async def test_a_corrected_period_replaces_the_old_figures(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [QUARTER]
    entry = analogue_entry()
    await setup_entry(hass, entry)

    fake_client.periods = [
        BillingPeriod(QUARTER.start, QUARTER.end, 91, 17.0, 67.35, QUARTER.recycled_kl, QUARTER.recycled_cost)
    ]
    await entry.runtime_data.async_poll_now()
    await hass.async_block_till_done()

    assert float(sensor_state(hass, entry, "period_usage").state) == 17.0
    assert float(sensor_state(hass, entry, "period_usage_charge").state) == 67.35


async def test_account_with_no_completed_period_has_unknown_figures(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.periods = []
    entry = analogue_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    for key in ("period_usage", "period_daily_average", "period_usage_charge"):
        assert sensor_state(hass, entry, key).state == "unknown"


async def test_portal_error_leaves_the_entry_retrying(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods_error = SewConnectionError("down")
    entry = analogue_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unreadable_meter_reads_do_not_hide_the_billed_figures(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.periods = [QUARTER]
    fake_client.reads_error = SewProtocolError("Meter not found among the property's meters")
    entry = analogue_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert float(sensor_state(hass, entry, "period_usage").state) == 16.0
    assert sensor_state(hass, entry, "period_usage").attributes["estimated_read"] is None


@pytest.mark.parametrize("error", [SewConnectionError("HTTP 503"), SewBusyError("throttled")], ids=["5xx", "busy"])
async def test_transient_meter_read_failure_does_not_hide_the_billed_figures(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception
) -> None:
    fake_client.periods = [QUARTER]
    fake_client.reads_error = error
    entry = analogue_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert float(sensor_state(hass, entry, "period_usage").state) == 16.0


async def test_session_rejected_during_meter_reads_still_starts_reauth(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.periods = [QUARTER]
    fake_client.reads_error = SewAuthError("Not logged in")
    entry = analogue_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_drinking_and_recycled_entries_are_independent(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.periods = [QUARTER]
    drinking = analogue_entry("mains", ANALOGUE_METER_ID)
    recycled = analogue_entry("recycled", OTHER_ANALOGUE_METER_ID)
    await setup_entry(hass, drinking)
    await setup_entry(hass, recycled)

    assert drinking.state is ConfigEntryState.LOADED
    assert recycled.state is ConfigEntryState.LOADED
    assert float(sensor_state(hass, drinking, "period_usage").state) == 16.0
    assert float(sensor_state(hass, recycled, "period_usage").state) == 5.0

    await hass.config_entries.async_unload(recycled.entry_id)
    assert drinking.state is ConfigEntryState.LOADED
    assert float(sensor_state(hass, drinking, "period_usage").state) == 16.0


async def test_import_from_date_skips_meters_without_hourly_history(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.periods = [QUARTER]
    entry = analogue_entry()
    await setup_entry(hass, entry)
    await hass.services.async_call(DOMAIN, "import_from_date", {"start_date": "2026-01-01"}, blocking=True)
    assert fake_client.fetch_ranges == []


async def test_config_flow_offers_a_meter_without_hourly_data_and_skips_history(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    fake_client.meters = [AccountIds(BILLING_ACCOUNT_ID, ANALOGUE_METER_ID, "SAHA000001", digital=False)]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    assert result["step_id"] == "meter"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_METER_ID: f"{BILLING_ACCOUNT_ID}:{ANALOGUE_METER_ID}", CONF_SUPPLY_TYPE: "recycled"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DIGITAL_METER] is False
    assert result["data"][CONF_SUPPLY_TYPE] == "recycled"
    assert result["title"] == f"{USERNAME} (recycled)"
