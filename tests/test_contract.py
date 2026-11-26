"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ics_courier.const import ParcelStatus
from custom_components.ics_courier.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

ZONE = ZoneInfo("America/Toronto")

SAMPLES = [active_sample(), delivered_sample()]


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("history", [False, True])
def test_contract_holds(sample, history):
    parcel = normalize_parcel(sample, include_history=history)
    assert list(parcel) == CANONICAL_KEYS
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None
    if parcel["planned_to"]:
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert (parcel["history"] is None) is (not history)


def test_delivered_parcel_stays_not_pickup():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-03T14:27:37"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_moment_ends_at_23_59_59_local():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    assert start == "2099-04-29T13:00:00Z"
    assert parse_iso(end).astimezone(ZONE).isoformat() == "2099-04-29T23:59:59-04:00"


def test_moment_at_end_of_day_has_no_end():
    assert planned_window("2099-04-29T23:59:59", None, zone=ZONE)[1] is None


def test_date_is_the_whole_local_day():
    start, end = planned_window("2099-04-29", None, zone=ZONE)
    assert start == "2099-04-29T00:00:00-04:00"
    assert end == "2099-04-29T23:59:59-04:00"


def test_two_dates_span_first_to_last_day():
    start, end = planned_window("2099-04-29", "2099-05-01", zone=ZONE)
    assert start == "2099-04-29T00:00:00-04:00"
    assert end == "2099-05-01T23:59:59-04:00"


def test_window_keeps_both_ends_and_swaps_reversed():
    assert planned_window(
        "2099-04-29T09:00:00+00:00", "2099-04-29T12:00:00+00:00", zone=UTC
    ) == ("2099-04-29T09:00:00+00:00", "2099-04-29T12:00:00+00:00")
    assert planned_window(
        "2099-04-29T12:00:00+00:00", "2099-04-29T09:00:00+00:00", zone=UTC
    ) == ("2099-04-29T09:00:00+00:00", "2099-04-29T12:00:00+00:00")


def test_window_without_start_or_with_garbage():
    assert planned_window(None, None, zone=UTC) == (None, None)
    assert planned_window("garbage", None, zone=UTC) == (None, None)
    assert planned_window(None, "2099-04-29", zone=ZONE)[0] == (
        "2099-04-29T00:00:00-04:00"
    )
    assert planned_window(datetime(2099, 4, 29, 8), None, zone=UTC)[0] == (
        "2099-04-29T08:00:00+00:00"
    )


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_window_emptied_at_destination(status):
    assert planned_for_status(status, "a", "b") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def test_pickup_route_survives_collection():
    collected = [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT]
    assert is_pickup(None, ParcelStatus.DELIVERED, collected) is True
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None]) is False


def test_pickup_is_derived_from_statuses(monkeypatch):
    monkeypatch.setattr(
        "custom_components.ics_courier.parcels.map_parcel_status",
        lambda code: ParcelStatus.AT_PICKUP_POINT,
    )
    parcel = normalize_parcel(active_sample())
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None
