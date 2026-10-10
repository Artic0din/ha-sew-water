"""Regression tests for explicit supply selection and independent histories."""

from datetime import timedelta
from hashlib import sha256
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.sew_water.const import CONF_METER_ID, CONF_STATISTIC_ID, CONF_SUPPLY_TYPE, DOMAIN
from custom_components.sew_water.sew_client import AccountIds, LoginResult

from .conftest import BACKFILL_DAYS, BILLING_ACCOUNT_ID, METER_ID, PASSWORD, USERNAME


async def start_selection(hass, fake_client):
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


async def select(hass, result, meter_id=METER_ID, supply="mains"):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_METER_ID: f"{BILLING_ACCOUNT_ID}:{meter_id}", CONF_SUPPLY_TYPE: supply},
    )


async def test_setup_requires_explicit_meter_selection(hass, fake_client):
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "meter"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_METER_ID: f"{BILLING_ACCOUNT_ID}:{METER_ID}", "supply_type": "recycled"}
    )
    assert result["step_id"] == "history"


async def test_choose_second_meter_and_reject_unknown_selection(hass, fake_client):
    fake_client.meters = [AccountIds(BILLING_ACCOUNT_ID, "recycled-id", "RECYCLED"), fake_client.ids]
    result = await start_selection(hass, fake_client)
    with pytest.raises(InvalidData, match="meter_id"):
        await select(hass, result, "not-listed")
    result = await select(hass, result)
    with patch("custom_components.sew_water.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_METER_ID] == METER_ID
    assert result["data"][CONF_SUPPLY_TYPE] == "mains"


async def test_duplicate_legacy_meter_is_rejected(hass, fake_client, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    result = await select(hass, await start_selection(hass, fake_client))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_preserves_selected_meter_when_order_changes(hass, fake_client, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    fake_client.login_result = LoginResult(mfa_required=False, channels=())
    fake_client.meters = [AccountIds(BILLING_ACCOUNT_ID, "other-id", "OTHER"), fake_client.ids]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": mock_config_entry.entry_id},
        data=mock_config_entry.data,
    )
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_METER_ID] == METER_ID


async def test_two_meters_have_independent_recorded_totals(hass, fake_client, mock_config_entry):
    entries = []
    for meter_id, supply, hourly_litres in [("drinking-id", "mains", 10), ("recycled-id", "recycled", 2)]:
        meter_key = sha256(meter_id.encode()).hexdigest()
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=meter_key,
            version=2,
            data={
                **mock_config_entry.data,
                CONF_METER_ID: meter_id,
                CONF_SUPPLY_TYPE: supply,
                CONF_STATISTIC_ID: f"{DOMAIN}:water_usage_{meter_key}",
            },
        )
        entry.add_to_hass(hass)
        fake_client.litres_per_hour = lambda _day, value=hourly_litres: value
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
        entries.append(entry)
    ids = {e.data[CONF_STATISTIC_ID] for e in entries}
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=10), None, ids, "hour", None, {"sum"}
    )
    assert rows[entries[0].data[CONF_STATISTIC_ID]][-1]["sum"] == BACKFILL_DAYS * 24 * 10
    assert rows[entries[1].data[CONF_STATISTIC_ID]][-1]["sum"] == BACKFILL_DAYS * 24 * 2
    assert hass.states.get("sensor.south_east_water_drinking_water_daily_usage") is not None
    assert hass.states.get("sensor.south_east_water_recycled_water_daily_usage") is not None
