"""Tests for the ICS Courier SOAP client."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ics_courier.api import (
    ICSCourierApiClient,
    ICSCourierApiError,
    build_envelope,
    parse_records,
)
from custom_components.ics_courier.const import SOAP_ACTION, TRACKING_API_URL

from .payloads import (
    NOT_FOUND_RECORD,
    delivered_record,
    record,
    soap_response,
)

CODE = "SYNTH0000000000002"


def _session(status: int = 200, body: str = "", headers: dict | None = None):
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


def test_envelope_carries_one_escaped_code_and_no_detail():
    body = build_envelope("A&B<1>")
    assert "<string>A&amp;B&lt;1&gt;</string>" in body
    assert body.count("<string>") == 1
    assert "<DetailInfo>false</DetailInfo>" in body
    assert 'xmlns="http://www.icscourier.ca/"' in body


async def test_request_shape():
    session = _session(body=soap_response([delivered_record()]))
    await ICSCourierApiClient(session).async_get_parcel(CODE)
    args, kwargs = session.post.call_args
    assert args[0] == TRACKING_API_URL
    assert kwargs["headers"]["SOAPAction"] == f'"{SOAP_ACTION}"'
    assert kwargs["headers"]["Content-Type"] == "text/xml; charset=utf-8"
    assert CODE in kwargs["data"]


async def test_returns_parcel_with_configured_code():
    session = _session(body=soap_response([delivered_record()]))
    parcel = await ICSCourierApiClient(session).async_get_parcel(CODE)
    assert parcel["trackingNumber"] == CODE
    assert parcel["events"][0]["Code"] == "DELIV"
    assert parcel["events"][0]["Sequence"] == "1"


@pytest.mark.parametrize("prefix", ["", "ics", "x"])
def test_parse_ignores_namespace_prefix(prefix):
    records = parse_records(soap_response([delivered_record()], prefix))
    assert len(records) == 1
    assert records[0]["Code"] == "DELIV"


def test_parse_returns_every_record():
    records = parse_records(
        soap_response([record("A", sequence=2), record("B", sequence=1)])
    )
    assert [r["Sequence"] for r in records] == ["2", "1"]


async def test_not_found_record_is_none():
    session = _session(body=soap_response([NOT_FOUND_RECORD]))
    assert await ICSCourierApiClient(session).async_get_parcel("X") is None


async def test_not_found_is_dropped_next_to_real_records():
    session = _session(body=soap_response([NOT_FOUND_RECORD, delivered_record()]))
    parcel = await ICSCourierApiClient(session).async_get_parcel(CODE)
    assert len(parcel["events"]) == 1


async def test_no_record_is_an_error():
    session = _session(body=soap_response([]))
    with pytest.raises(ICSCourierApiError):
        await ICSCourierApiClient(session).async_get_parcel(CODE)


async def test_soap_fault_is_an_error():
    fault = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><soap:Fault><faultstring>x</faultstring></soap:Fault>"
        "</soap:Body></soap:Envelope>"
    )
    with pytest.raises(ICSCourierApiError):
        await ICSCourierApiClient(_session(body=fault)).async_get_parcel(CODE)


async def test_malformed_xml_is_an_error():
    with pytest.raises(ICSCourierApiError):
        await ICSCourierApiClient(_session(body="<a><b>")).async_get_parcel(CODE)


async def test_doctype_is_rejected():
    body = '<!DOCTYPE x [<!ENTITY a "b">]><x/>'
    with pytest.raises(ICSCourierApiError):
        await ICSCourierApiClient(_session(body=body)).async_get_parcel(CODE)


async def test_non_2xx_is_an_error_with_status():
    with pytest.raises(ICSCourierApiError) as err:
        await ICSCourierApiClient(_session(500)).async_get_parcel(CODE)
    assert err.value.status_code == 500


@pytest.mark.parametrize(
    "header,expected", [("30", 30.0), (None, None), ("Wed, 21 Oct", None)]
)
async def test_429_reads_retry_after(header, expected):
    headers = {"Retry-After": header} if header else {}
    with pytest.raises(ICSCourierApiError) as err:
        await ICSCourierApiClient(_session(429, headers=headers)).async_get_parcel(
            CODE
        )
    assert err.value.status_code == 429
    assert err.value.retry_after == expected


async def test_network_error_propagates():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await ICSCourierApiClient(session).async_get_parcel(CODE)


async def test_concurrent_reads_of_one_code_share_a_request():
    session = _session(body=soap_response([delivered_record()]))
    client = ICSCourierApiClient(session)
    first, second = await asyncio.gather(
        client.async_get_parcel(CODE), client.async_get_parcel(CODE)
    )
    assert first == second
    assert session.post.call_count == 1
    await client.async_get_parcel(CODE)
    assert session.post.call_count == 2
