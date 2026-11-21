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

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
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
    floor = datetime.min.replace(tzinfo=UTC)
    unique.sort(key=lambda e: (_sequence(e), parse_iso(e.get("StatusDate")) or floor))
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
        head = re.sub(
            r"\s+at$", "", status.split(":", 1)[0].strip(), flags=re.IGNORECASE
        )
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


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``raw`` is ``{"trackingNumber": <configured code>, "events": [<record>]}``.
    The **keys of the returned dict are the contract** — every carrier in the
    suite returns exactly these, in this order. ICS exposes no ETA, party,
    weight or dimension fields, so those stay ``None``; ``pickup`` is derived
    from the statuses and ``pickup_point`` stays ``None`` without a real name.
    """
    events = ordered_events(raw.get("events"))
    latest = events[-1] if events else {}
    status = map_parcel_status(latest.get("Code"))
    delivered = status is ParcelStatus.DELIVERED
    event_statuses = [_STATUS_MAP.get(event.get("Code")) for event in events]

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
        "pickup": is_pickup(None, status, event_statuses),
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
