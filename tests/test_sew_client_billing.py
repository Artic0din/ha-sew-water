"""Tests for billing periods and meter reads, which work without hourly data."""

from __future__ import annotations

from datetime import date
import json
from typing import Any

from aioresponses import aioresponses
import pytest

from sew_client import (
    AccountIds,
    BillingPeriod,
    MeterRead,
    SewAuthError,
    SewClient,
    SewProtocolError,
    _parse_billing_periods,
    _parse_meter_reads,
)

from .conftest import BILLING_ACCOUNT_ID, METER_ID, aura_envelope
from .test_sew_client import AURA_URL, aura_message, mock_dead_home, mock_home, posted_form

PROPERTY_ID = "a07900000000PROPAAA"
ACCOUNT_NUMBER = "1234567"
SERIAL_DRINKING = "SAHA000001"
SERIAL_RECYCLED = "RAHA000002"
IDS = AccountIds(billing_account_id=BILLING_ACCOUNT_ID, meter_id=METER_ID, meter_serial=SERIAL_RECYCLED, digital=False)


def period_row(
    start: str | None,
    end: str | None,
    days: float | None,
    drinking: tuple[Any, float] = (0, 0),
    recycled: tuple[float, float] = (0, 0),
) -> dict[str, Any]:
    """One ``Consumption_and_Charge__c`` row as ``getConsumptionsAndChargesByBillingAccountIds`` returns it."""
    return {
        "Billing_Account__c": BILLING_ACCOUNT_ID,
        "Previous_Read_Date__c": start,
        "Current_Read_Date__c": end,
        "Number_of_Days__c": days,
        "Water_Volume__c": drinking[0],
        "Water_Cost__c": drinking[1],
        "Recycled_Water_Volume__c": recycled[0],
        "Recycled_Water_Cost__c": recycled[1],
    }


# Four quarters as the portal reported them for a drinking-and-recycled account, newest first.
QUARTERS = [
    period_row("2026-05-21", "2026-08-20", 91.0, (16, 63.25), (5, 11.98)),
    period_row("2026-02-23", "2026-05-21", 87.0, (15, 56.53), (3, 7.03)),
    period_row("2025-11-20", "2026-02-23", 95.0, (18, 67.83), (6, 14.06)),
    period_row("2025-08-19", "2025-11-20", 93.0, (20, 75.37), (5, 11.72)),
]


def read_row(read_date: str | None, reading: str, consumption: str, days: str, estimated: str) -> dict[str, Any]:
    """One row as ``getMeterReadings`` returns it, with the portal's space padding."""
    return {
        "readingMethod": "DA",
        "reading": reading,
        "readDate": read_date,
        "noOfDays": days,
        "estimated": estimated,
        "consumption": consumption,
        "averageConsumption": "55",
    }


READS = [
    read_row("2026-08-20", "        273 ", "          5.00 ", "    90 ", "NO "),
    read_row("2026-05-22", "        268 ", "          3.00 ", "    88 ", "YES"),
]


# ------------------------------------------------------------------ billing periods


def test_periods_are_returned_oldest_first_with_both_supplies() -> None:
    periods = _parse_billing_periods(QUARTERS)
    assert [p.end for p in periods] == [date(2025, 11, 20), date(2026, 2, 23), date(2026, 5, 21), date(2026, 8, 20)]
    assert periods[-1] == BillingPeriod(
        start=date(2026, 5, 21),
        end=date(2026, 8, 20),
        days=91,
        drinking_kl=16.0,
        drinking_cost=63.25,
        recycled_kl=5.0,
        recycled_cost=11.98,
    )


def test_periods_accept_the_json_string_the_portal_sends() -> None:
    assert _parse_billing_periods(json.dumps(QUARTERS)) == _parse_billing_periods(QUARTERS)


def test_empty_history_is_no_periods() -> None:
    assert _parse_billing_periods([]) == []


def test_period_without_both_read_dates_is_left_out() -> None:
    """The quarter in progress has no closing read; it must not become the latest period."""
    rows = [period_row("2026-08-20", None, None, (4, 9.99)), period_row(None, None, None), *QUARTERS]
    assert [p.end for p in _parse_billing_periods(rows)][-1] == date(2026, 8, 20)
    assert len(_parse_billing_periods(rows)) == 4


def test_correction_replaces_the_period_instead_of_adding_to_it() -> None:
    corrected = period_row("2026-05-21", "2026-08-20", 91.0, (17, 67.35), (5, 11.98))
    periods = _parse_billing_periods([*QUARTERS, corrected])
    assert len(periods) == 4
    assert periods[-1].drinking_kl == 17.0
    assert periods[-1].drinking_cost == 67.35


def test_repeated_parsing_gives_the_same_periods() -> None:
    assert _parse_billing_periods(QUARTERS) == _parse_billing_periods(QUARTERS)


def test_missing_supply_figures_are_zero() -> None:
    """An account with no recycled supply reports nulls for it."""
    row = period_row("2026-05-21", "2026-08-20", 91.0, (16, 63.25))
    row["Recycled_Water_Volume__c"] = None
    row["Recycled_Water_Cost__c"] = None
    (period,) = _parse_billing_periods([row])
    assert (period.recycled_kl, period.recycled_cost) == (0.0, 0.0)


def test_missing_day_count_is_taken_from_the_dates() -> None:
    (period,) = _parse_billing_periods([period_row("2026-05-21", "2026-08-20", None)])
    assert period.days == 91


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        None,
        {"records": []},
        ["not an object"],
    ],
    ids=["bad-string", "null", "object", "bad-row"],
)
def test_malformed_period_data_is_rejected(value: Any) -> None:
    with pytest.raises(SewProtocolError):
        _parse_billing_periods(value)


@pytest.mark.parametrize(
    "row",
    [
        period_row("2026-05-21", "2026-08-20", 91.0, (-1, 5.0)),
        period_row("2026-05-21", "2026-08-20", 91.0, ("lots", 5.0)),
        period_row("2026-05-21", "2026-08-20", 91.0, (True, 5.0)),
        period_row("2026-05-21", "2026-08-20", 91.0, (float("nan"), 5.0)),
        period_row("2026-08-20", "2026-05-21", 91.0),
        period_row("2026-05-21", "2026-05-21", 0.0),
        period_row("21/05/2026", "2026-08-20", 91.0),
        period_row("2026-05-21", "2026-08-20", 0.0, (5, 5.0)),
    ],
    ids=["negative", "text", "bool", "nan", "reversed", "empty-span", "bad-date", "zero-days"],
)
def test_implausible_period_row_is_rejected(row: dict[str, Any]) -> None:
    with pytest.raises(SewProtocolError):
        _parse_billing_periods([row])


async def test_fetch_billing_periods_asks_for_the_selected_account(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope({"cacheable": False, "returnValue": json.dumps(QUARTERS)}))
    periods = await client.async_fetch_billing_periods(IDS)
    assert periods[-1].recycled_kl == 5.0

    (action,) = aura_message(posted_form(mocked, "/s/sfsites/aura"))["actions"]
    assert action["descriptor"] == "aura://ApexActionController/ACTION$execute"
    assert action["params"] == {
        "cacheable": False,
        "classname": "cm_AccountBillingUsageAURA",
        "isContinuation": False,
        "method": "getConsumptionsAndChargesByBillingAccountIds",
        "namespace": "",
        "params": {"billingAccIds": [BILLING_ACCOUNT_ID]},
    }


async def test_fetch_billing_periods_without_a_session_is_auth_error(client: SewClient, mocked: aioresponses) -> None:
    mock_dead_home(mocked)
    with pytest.raises(SewAuthError):
        await client.async_fetch_billing_periods(IDS)


async def test_fetch_billing_periods_failed_action_is_protocol_error(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(None, state="ERROR"))
    with pytest.raises(SewProtocolError):
        await client.async_fetch_billing_periods(IDS)


# -------------------------------------------------------------------- meter reads


def test_reads_are_parsed_from_padded_strings_oldest_first() -> None:
    reads = _parse_meter_reads(READS)
    assert reads == [
        MeterRead(date(2026, 5, 22), 268, 3.0, 88, estimated=True),
        MeterRead(date(2026, 8, 20), 273, 5.0, 90, estimated=False),
    ]


def test_read_without_a_date_is_left_out_and_unknown_status_is_none() -> None:
    reads = _parse_meter_reads([read_row(None, "1", "1.00", "1", "NO"), read_row("2026-08-20", "273", "5", "90", "?")])
    assert [r.estimated for r in reads] == [None]


def test_repeated_read_date_keeps_one_read() -> None:
    reads = _parse_meter_reads([*READS, read_row("2026-08-20", "274", "6.00", "90", "NO")])
    assert [(r.read_date, r.reading) for r in reads] == [(date(2026, 5, 22), 268), (date(2026, 8, 20), 274)]


@pytest.mark.parametrize("value", ["{", None, {"a": 1}, [1]], ids=["bad-string", "null", "object", "bad-row"])
def test_malformed_read_data_is_rejected(value: Any) -> None:
    with pytest.raises(SewProtocolError):
        _parse_meter_reads(value)


def test_read_without_a_reading_is_rejected() -> None:
    with pytest.raises(SewProtocolError, match="reading"):
        _parse_meter_reads([read_row("2026-08-20", "", "5.00", "90", "NO")])


def account_list() -> dict[str, Any]:
    """``getBillingAccountsForUser`` with the fields the read lookup needs."""
    return {
        "cacheable": False,
        "returnValue": json.dumps([{"Id": BILLING_ACCOUNT_ID, "Name": ACCOUNT_NUMBER, "Property__c": PROPERTY_ID}]),
    }


def meter_list(*serials: str) -> dict[str, Any]:
    """``getMeterAndReadings``: every meter on the property is type ``W`` whatever it carries."""
    meters = [
        {"Id": f"a9K0000000{i}", "Name": serial, "PitReference": f"M000000{i}", "MeterType": "W"}
        for i, serial in enumerate(serials)
    ]
    return {"cacheable": False, "returnValue": json.dumps(meters)}


async def test_fetch_meter_reads_looks_up_the_selected_meter(client: SewClient, mocked: aioresponses) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(account_list()))
    mocked.post(AURA_URL, status=200, payload=aura_envelope(meter_list(SERIAL_DRINKING, SERIAL_RECYCLED)))
    mocked.post(AURA_URL, status=200, payload=aura_envelope({"cacheable": False, "returnValue": json.dumps(READS)}))
    reads = await client.async_fetch_meter_reads(IDS)
    assert [r.reading for r in reads] == [268, 273]

    methods = [aura_message(posted_form(mocked, "/s/sfsites/aura", i))["actions"][0]["params"] for i in range(3)]
    assert [m["method"] for m in methods] == ["getBillingAccountsForUser", "getMeterAndReadings", "getMeterReadings"]
    assert {m["classname"] for m in methods} == {"cm_AccountBillingUsageAURA"}
    assert methods[1]["params"] == {"property": PROPERTY_ID, "acctNum": ACCOUNT_NUMBER}
    # The recycled meter is the second one listed, so its own pit reference must be requested.
    assert methods[2]["params"] == {"aMeterType": "W", "aMeterPit": "M0000001", "aAccountNumber": ACCOUNT_NUMBER}


async def test_fetch_meter_reads_for_a_meter_not_on_the_property_is_protocol_error(
    client: SewClient, mocked: aioresponses
) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope(account_list()))
    mocked.post(AURA_URL, status=200, payload=aura_envelope(meter_list(SERIAL_DRINKING)))
    with pytest.raises(SewProtocolError, match="Meter not found"):
        await client.async_fetch_meter_reads(IDS)


async def test_fetch_meter_reads_for_an_unknown_account_is_protocol_error(
    client: SewClient, mocked: aioresponses
) -> None:
    mock_home(mocked)
    mocked.post(AURA_URL, status=200, payload=aura_envelope({"cacheable": False, "returnValue": "[]"}))
    with pytest.raises(SewProtocolError, match="account number"):
        await client.async_fetch_meter_reads(IDS)


async def test_fetch_meter_reads_without_a_serial_makes_no_request(client: SewClient, mocked: aioresponses) -> None:
    with pytest.raises(SewProtocolError, match="serial"):
        await client.async_fetch_meter_reads(AccountIds(BILLING_ACCOUNT_ID, METER_ID, None, digital=False))
    assert not mocked.requests
