"""Tests for ICS Courier diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ics_courier.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EXAMPLE123456"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "trackingNumber": "EXAMPLE123456",
                "recipient": "Jane Doe",
                "deliveryAddress": {"city": "Rotterdam", "street": "Coolsingel 1"},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipient"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryAddress"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


async def test_diagnostics_redacts_a_normalised_delivered_record(hass):
    """Status text embeds the recipient's name and address; none may survive."""
    from custom_components.ics_courier.parcels import normalize_parcel

    from .payloads import delivered_sample

    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=45)
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = [
        normalize_parcel(delivered_sample(), include_history=True)
    ]
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    text = str(result)
    for leaked in ("JANE", "TEST ST", "A1A1A1", "SAMPLE", "SYNTH0000000000002"):
        assert leaked not in text
    event = result["delivered"][0]["raw"]["events"][0]
    for key in ("TrackingNum", "Status", "StatusFrench", "Signby"):
        assert event[key] == "**REDACTED**"
    assert event["Code"] == "DELIV"
