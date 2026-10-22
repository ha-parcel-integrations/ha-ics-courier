"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the carrier-specific mapping is kept
free of I/O so it can be tested as plain functions.
"""

from datetime import UTC, datetime, timedelta
import logging

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ics_courier.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ics_courier.parcels import (
    apply_delivered_filter,
    build_history,
    describe_event,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    ordered_events,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    DELIVERED_STATUS,
    active_sample,
    delivered_record,
    delivered_sample,
    record,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


def test_deliv_is_the_only_mapped_code():
    assert map_parcel_status("DELIV") == ParcelStatus.DELIVERED


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("SYNTH") == ParcelStatus.UNKNOWN


def test_missing_code_is_unknown_and_warns_once(caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        assert map_parcel_status(None) == ParcelStatus.UNKNOWN
        assert map_parcel_status("") == ParcelStatus.UNKNOWN
    assert caplog.text.count("status=(empty)") == 1


def test_map_event_status_missing_and_unmapped_are_none():
    assert map_event_status(None) is None
    assert map_event_status("SYNTH") is None
    assert map_event_status("DELIV") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_once_and_logs_only_the_code(caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        map_parcel_status("SYNTH")
        map_event_status("SYNTH")
    assert caplog.text.count("status=SYNTH") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == UTC
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None


# ---------------------------------------------------------------------------
# describe_event / ordered_events / build_history
# ---------------------------------------------------------------------------


def test_describe_event_strips_everything_after_the_colon():
    label = describe_event(delivered_record())
    assert label == "Delivered"
    assert "JANE" not in label


def test_describe_event_french_style_head_without_at():
    assert describe_event({"Status": "Delivered: X", "Code": "C"}) == "Delivered"


def test_describe_event_without_colon_uses_code_not_free_text():
    assert describe_event({"Status": "Somewhere 1 Test St", "Code": "SYNTH"}) == "SYNTH"
    assert describe_event({"Status": "", "Code": ""}) is None


def test_describe_event_empty_head_falls_back_to_code():
    assert describe_event({"Status": ": hidden", "Code": "SYNTH"}) == "SYNTH"


def test_ordered_events_sorts_by_sequence_then_date_and_drops_duplicates():
    late = record("B", "2026-01-02T12:00:00", sequence=2)
    early = record("A", "2026-01-02T09:00:00", sequence=2)
    first = record("C", "2026-01-05T09:00:00", sequence=1)
    ordered = ordered_events([late, early, first, dict(early), "junk"])
    assert [e["Code"] for e in ordered] == ["C", "A", "B"]


def test_ordered_events_tolerates_bad_sequence_and_date():
    odd = record("X", "garbage", sequence=1)
    odd["Sequence"] = "n/a"
    assert ordered_events([odd, record("Y")])[0]["Code"] == "X"
    assert ordered_events(None) == []


def test_build_history_is_oldest_to_newest_with_safe_labels():
    history = build_history(
        [delivered_record(), record("SYNTH", "2026-01-01T08:00:00", sequence=0)]
    )
    assert [h["status"] for h in history] == [None, ParcelStatus.DELIVERED]
    assert history[-1]["raw_status"] == "Delivered"
    assert history[-1]["timestamp"] == "2026-01-03T14:27:37"


def test_build_history_caps_to_max_events():
    events = [
        record("SYNTH", f"2026-01-{day:02d}T10:00:00", sequence=day)
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_skips_records_without_a_date():
    assert build_history(None) == []
    assert build_history([record("SYNTH", "")]) == []


def test_build_history_reports_a_time_it_cannot_read_once(caplog):
    events = [
        record("SYNTH", "yesterday afternoon", sequence=1),
        record("SYNTH", "the day before", sequence=2),
    ]
    with caplog.at_level(logging.WARNING):
        history = build_history(events)
    assert len(history) == 2  # the entry is kept, just not ordered on its time
    assert caplog.text.count("cannot read") == 1
    assert "StatusDate" in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    assert "weight" not in CAPABILITIES and delivered["weight"] is None
    assert "dimensions" not in CAPABILITIES and delivered["dimensions"] is None
    assert "delivery_window" not in CAPABILITIES
    assert delivered["planned_from"] is None and delivered["planned_to"] is None
    assert "pickup_point" not in CAPABILITIES and delivered["pickup_point"] is None
    assert "url" not in CAPABILITIES and delivered["url"] is None
    assert "history" in CAPABILITIES and with_history["history"] is not None


def test_normalize_delivered_parcel_exposes_no_recipient_text():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "ICS Courier"
    assert parcel["barcode"] == "SYNTH0000000000002"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-03T14:27:37"
    assert parcel["sender"] is None and parcel["receiver"] is None
    assert parcel["history"] is None
    public = {k: v for k, v in parcel.items() if k != "raw"}
    assert "JANE" not in str(public)


def test_normalize_keeps_the_full_untrimmed_record_in_raw():
    sample = delivered_sample()
    parcel = normalize_parcel(sample)
    assert parcel["raw"] is sample
    assert parcel["raw"]["events"][0]["Status"] == DELIVERED_STATUS


def test_normalize_history_is_a_single_event_list():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 1
    assert parcel["history"][0]["status"] == ParcelStatus.DELIVERED


def test_normalize_unmapped_code_is_unknown_and_not_delivered():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert (
        parcel["raw_status"] == "Synthetic in-flight status"
        or parcel["raw_status"] == "SYNTH"
    )


def test_signby_is_never_delivery_proof():
    sample = active_sample()
    sample["events"][0]["Signby"] = "SAMPLE"
    assert normalize_parcel(sample)["delivered"] is False


def test_band_ordering_newest_sequence_wins():
    """A later non-delivered record must not be masked by an earlier DELIV."""
    sample = {
        "trackingNumber": "SYNTH0000000000009",
        "events": [
            record("SYNTH", "2026-01-02T10:00:00", sequence=2),
            delivered_record("SYNTH0000000000009") | {"Sequence": "1"},
        ],
    }
    assert normalize_parcel(sample)["status"] == ParcelStatus.UNKNOWN


def test_normalize_pending_placeholder():
    """A tracked-but-unknown code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "SYNTH0000000000003"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
