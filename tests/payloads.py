"""Synthetic ICS Courier payloads shared by the test modules.

Every identifier, name, address and code here is invented. ``DELIV`` is the
only status code the integration maps; ``SYNTH`` stands in for any other code.
"""
from __future__ import annotations

ACTIVE_CODE = "SYNTH0000000000001"
DELIVERED_CODE = "SYNTH0000000000002"

DELIVERED_STATUS = "Delivered at: JANE SAMPLE  1 TEST ST  EXAMPLETOWN ON A1A1A1"
DELIVERED_STATUS_FR = "Livré à : JANE SAMPLE  1 TEST ST  EXAMPLETOWN ON A1A1A1"


def record(
    code: str,
    status_date: str = "2026-01-02T10:00:00",
    status: str = "In progress",
    sequence: int = 1,
    tracking: str = ACTIVE_CODE,
) -> dict:
    """One flat ``TrackingInfoWithSeq`` record."""
    return {
        "TrackingNum": tracking,
        "StatusDate": status_date,
        "Status": status,
        "Signby": "",
        "Code": code,
        "StatusFrench": status,
        "Sequence": str(sequence),
    }


def delivered_record(tracking: str = DELIVERED_CODE) -> dict:
    """The record shape a delivered parcel returns."""
    return {
        "TrackingNum": tracking,
        "StatusDate": "2026-01-03T14:27:37",
        "Status": DELIVERED_STATUS,
        "Signby": "SAMPLE",
        "Code": "DELIV",
        "StatusFrench": DELIVERED_STATUS_FR,
        "Sequence": "1",
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A parcel dict as ``async_get_parcel`` returns it, delivered."""
    return {"trackingNumber": code, "events": [delivered_record(code)]}


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel carrying a code the integration does not map."""
    return {
        "trackingNumber": code,
        "events": [record("SYNTH", status="Synthetic in-flight status", tracking=code)],
    }


def out_for_delivery_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel carrying the invented ``SYNTHOUT`` code."""
    return {
        "trackingNumber": code,
        "events": [record("SYNTHOUT", "2026-01-02T12:00:00", sequence=2, tracking=code)],
    }


def soap_response(records: list[dict], prefix: str = "") -> str:
    """The doubly-nested SOAP response, with an optional namespace prefix."""
    tag = f"{prefix}:" if prefix else ""
    xmlns = f"xmlns:{prefix}" if prefix else "xmlns"
    inner = "".join(
        "<{t}TrackingInfoWithSeq>{fields}</{t}TrackingInfoWithSeq>".format(
            t=tag,
            fields="".join(f"<{tag}{k}>{v}</{tag}{k}>" for k, v in rec.items()),
        )
        for rec in records
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{tag}TracePackgeWithSequenceResponse {xmlns}="http://www.icscourier.ca/">'
        f"<{tag}TracePackgeWithSequenceResult>"
        f"<{tag}TrackingInfoWithSeq>{inner}</{tag}TrackingInfoWithSeq>"
        f"</{tag}TracePackgeWithSequenceResult>"
        f"</{tag}TracePackgeWithSequenceResponse></soap:Body></soap:Envelope>"
    )


NOT_FOUND_RECORD = {
    "TrackingNum": "SYNTHMISSING",
    "StatusDate": "2026-01-02T10:00:00",
    "Status": "Not Found",
    "Signby": "",
    "Code": "",
    "StatusFrench": "Non trouvé",
    "Sequence": "0",
}
