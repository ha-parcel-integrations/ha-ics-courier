"""ICS Courier SOAP tracking client.

Contract the coordinator relies on:

* ``async_get_parcel`` returns ``{"trackingNumber": code, "events": [...]}``
  on success, where each event is the flat dict of one ``TrackingInfoWithSeq``
  record,
* returns ``None`` when the carrier reports the code as unknown (a normal
  state — never an error),
* raises :class:`ICSCourierApiError` for anything else, with ``status_code``
  set on a non-2xx response and ``retry_after`` set when a 429 carried a
  ``Retry-After`` in seconds,
* lets ``aiohttp.ClientError`` propagate untouched.
"""
from __future__ import annotations

import asyncio
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import aiohttp

from .const import SOAP_ACTION, SOAP_NAMESPACE, TRACKING_API_URL

_RECORD = "TrackingInfoWithSeq"


class ICSCourierApiError(Exception):
    """Raised when an ICS Courier API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"ICS Courier API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def build_envelope(tracking_code: str) -> str:
    """Return the SOAP 1.1 request body for one tracking code."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<TracePackgeWithSequence xmlns="{SOAP_NAMESPACE}">'
        f"<TrackNums><string>{escape(tracking_code)}</string></TrackNums>"
        "<DetailInfo>false</DetailInfo>"
        "</TracePackgeWithSequence>"
        "</soap:Body></soap:Envelope>"
    )


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rpartition("}")[2]


def parse_records(body: str) -> list[dict[str, str]]:
    """Return the flat record dicts of a SOAP response, in document order.

    Matches on local names so namespace prefixes do not matter. The response
    nests ``TrackingInfoWithSeq`` inside itself; only the innermost element,
    the one that carries the fields, is a record. A SOAP fault raises.
    """
    if "<!DOCTYPE" in body.upper():
        raise ICSCourierApiError("unexpected DTD in response")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as err:
        raise ICSCourierApiError("unparseable XML body") from err

    records: list[dict[str, str]] = []
    for element in root.iter():
        name = _local(element.tag)
        if name == "Fault":
            raise ICSCourierApiError("SOAP fault")
        if name != _RECORD:
            continue
        fields = {
            _local(child.tag): (child.text or "").strip()
            for child in element
            if len(child) == 0
        }
        if fields:
            records.append(fields)
    return records


def _is_not_found(record: dict[str, str]) -> bool:
    """Return True for the semantic "unknown code" record."""
    return (
        record.get("Status", "").strip().lower() == "not found"
        and not record.get("Code")
    )


class ICSCourierApiClient:
    """Client for the anonymous ICS Courier trace operation."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._inflight: dict[str, asyncio.Task] = {}

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's latest status.

        Concurrent calls for the same code share one request.
        """
        task = self._inflight.get(tracking_code)
        if task is None:
            task = asyncio.ensure_future(self._fetch(tracking_code))
            self._inflight[tracking_code] = task
            task.add_done_callback(lambda _: self._inflight.pop(tracking_code, None))
        return await asyncio.shield(task)

    async def _fetch(self, tracking_code: str) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{SOAP_ACTION}"',
        }
        async with self._session.post(
            TRACKING_API_URL, data=build_envelope(tracking_code), headers=headers
        ) as response:
            if response.status == 429:
                header = response.headers.get("Retry-After")
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds
                raise ICSCourierApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise ICSCourierApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            body = await response.text()

        records = parse_records(body)
        if not records:
            raise ICSCourierApiError("no matching record in response")
        events = [record for record in records if not _is_not_found(record)]
        if not events:
            return None
        return {"trackingNumber": tracking_code, "events": events}
