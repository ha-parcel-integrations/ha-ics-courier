"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific parts are :data:`_STATUS_MAP`, :func:`build_history` and
:func:`normalize_parcel`. The endpoint reports the latest status only, as a
list of records keyed by ``Sequence``; the newest one drives the parcel.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ics-courier/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the record's ``Code``. Only codes seen on a real parcel are mapped;
# the French/English display text embeds the recipient's name and address, so
# it is never used for mapping.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "DELIV": ParcelStatus.DELIVERED,
}

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()
_unreadable_times_logged: set[str] = set()

# The one field a record dates itself with.
_TIMESTAMP_FIELD = "StatusDate"


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised ICS Courier status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    Not-found records never reach this point, so a record without a code is an
    unconfirmed shape: it reports ``unknown`` with the same one-shot warning as
    an unrecognised code.
    """
    if not code:
        _warn_unmapped_status("(empty)")
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def _warn_unreadable_timestamp() -> None:
    """Report once that a record's own timestamp could not be read."""
    if _TIMESTAMP_FIELD in _unreadable_times_logged:
        return
    _unreadable_times_logged.add(_TIMESTAMP_FIELD)
    _LOGGER.warning(
        "ICS Courier sent a time this integration cannot read, so the "
        "parcel's history may be ordered wrongly. Open an issue and paste "
        "this line: %s\n  field=%s",
        NEW_ISSUE_URL,
        _TIMESTAMP_FIELD,
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def _sequence(event: dict) -> int:
    """Return the record's ``Sequence`` as an int, 0 when absent or garbled."""
    try:
        return int(str(event.get("Sequence", "")).strip())
    except ValueError:
        return 0


def ordered_events(events: list | None) -> list[dict]:
    """Return the records oldest -> newest, dropping exact duplicates.

    Sorted on ``Sequence`` first, then the parsed ``StatusDate``; a record
    without a parseable date sorts before dated ones of the same sequence.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        key = (
            str(event.get("Sequence", "")),
            str(event.get("Code", "")),
            str(event.get("StatusDate", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    unique.sort(
        key=lambda e: (_sequence(e), parse_iso(e.get("StatusDate")) or floor)
    )
    return unique


def describe_event(event: dict) -> str | None:
    """Return a privacy-safe label for a record.

    ``Status`` reads like ``Delivered at: <name> <address>``, so only the
    words before the colon (minus a trailing "at") are kept; without a colon
    the opaque ``Code`` is used instead of risking the free text.
    """
    status = str(event.get("Status") or "")
    code = event.get("Code") or None
    if ":" in status:
        head = re.sub(r"\s+at$", "", status.split(":", 1)[0].strip(), flags=re.I)
        return head or code
    return code


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's records.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Sorted oldest -> newest and capped to the
    most recent ``max_events``. ``StatusDate`` carries no timezone (Canadian
    local time) and is published as received, without inventing an offset.
    """
    history: list[dict] = []
    for event in ordered_events(events):
        timestamp = event.get("StatusDate")
        if not timestamp:
            continue
        if parse_iso(timestamp) is None:
            _warn_unreadable_timestamp()
        history.append(
            {
                "timestamp": timestamp,
                "status": map_event_status(event.get("Code")),
                "raw_status": describe_event(event),
            }
        )
    return history[-max_events:]


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``raw`` is ``{"trackingNumber": <configured code>, "events": [<record>]}``.
    The **keys of the returned dict are the contract** — every carrier in the
    suite returns exactly these, in this order. ICS exposes no ETA, party,
    pickup, weight or dimension fields, so those stay ``None``.
    """
    events = ordered_events(raw.get("events"))
    latest = events[-1] if events else {}
    status = map_parcel_status(latest.get("Code"))
    delivered = status is ParcelStatus.DELIVERED

    return {
        "carrier": "ICS Courier",
        "barcode": raw.get("trackingNumber"),
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": describe_event(latest) if latest else None,
        "delivered": delivered,
        "delivered_at": (latest.get("StatusDate") or None) if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
