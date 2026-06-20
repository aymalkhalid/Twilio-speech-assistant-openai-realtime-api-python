"""Focused unit tests for webhook-service business-record helpers."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.webhook_service import (
    _derive_primary_booking_projection,
    _merge_business_metadata,
    _upsert_business_appointment,
    handoff_payload_to_supabase_updates,
    sync_business_lead_after_booking_action_sync,
    update_business_lead_from_payload_sync,
)
from config import Config
import services.webhook_service as webhook_service_module


def test_handoff_payload_to_supabase_updates_filters_nulls():
    payload = {
        "company_name": "Prestige Cleaning Services",
        "industry": "cleaning",
        "priority": "routine",
        "contact": {
            "name": "Emil",
            "phone": "+12185953061",
            "email": "",
        },
        "issue_summary": "Cancellation request",
        "call_summary": "Caller requested cancellation.",
        "preferred_callback_time": None,
        "confirmed_slot": None,
        "transcript": None,
        "call_sid": "CA_followup_1",
        "timestamp": "2026-04-20T00:00:00+00:00",
        "calendar_event_link": None,
        "recording_link": None,
        "metadata": None,
        "service_address": None,
    }

    updates = handoff_payload_to_supabase_updates(payload)

    assert updates["lead_name"] == "Emil"
    assert updates["lead_phone"] == "+12185953061"
    assert updates["call_sid"] == "CA_followup_1"
    assert "confirmed_slot" not in updates
    assert "calendar_event_link" not in updates
    assert "recording_link" not in updates
    assert "metadata" not in updates
    assert "service_address" not in updates


def test_merge_business_metadata_archives_previous_interaction():
    existing_metadata = {
        "source": "lead_call",
        "booking_event_id": "evt_original",
        "related_call_sids": ["CA_original"],
    }
    archived_lead = {
        "call_sid": "CA_original",
        "timestamp": "2026-04-19T10:00:00+00:00",
        "issue_summary": "Move-out cleaning request",
        "call_summary": "Original booking confirmed.",
        "confirmed_slot": {"display": "Mon Apr 20 at 01:00 PM"},
        "calendar_event_link": "https://calendar.google.com/event?eid=evt_original",
        "recording_link": "https://api.twilio.com/Recordings/RE_original",
        "transcript": "Original transcript",
        "transcript_summary": "Original summary",
        "transcript_issues": "Original issues",
        "lead_status": "booked",
        "service_address": "Apartment 123",
    }

    merged = _merge_business_metadata(
        existing_metadata,
        booking_event_id="evt_original",
        booking_state="cancelled",
        call_sid="CA_followup",
        interaction_type="cancelled",
        archive_lead=archived_lead,
    )

    assert merged["source"] == "lead_call"
    assert merged["booking_event_id"] == "evt_original"
    assert merged["booking_state"] == "cancelled"
    assert merged["last_interaction_call_sid"] == "CA_followup"
    assert merged["last_interaction_type"] == "cancelled"
    assert merged["related_call_sids"] == ["CA_original", "CA_followup"]
    assert merged["primary_call_sid"] == "CA_original"
    assert merged["business_record_mode"] == "lifecycle"
    assert len(merged["interaction_history"]) == 1
    assert merged["interaction_history"][0]["call_sid"] == "CA_original"
    assert merged["interaction_history"][0]["call_summary"] == "Original booking confirmed."


def test_upsert_business_appointment_preserves_cancelled_slot_history():
    appointments = [
        {
            "event_id": "evt_original",
            "state": "booked",
            "confirmed_slot": {"display": "Mon Apr 20 at 01:00 PM", "start": "2026-04-20T18:00:00Z"},
            "calendar_event_link": "https://calendar.google.com/event?eid=evt_original",
            "summary": "Move-out cleaning",
            "service_address": "Apartment 123",
        }
    ]

    updated = _upsert_business_appointment(
        appointments,
        event_id="evt_original",
        state="cancelled",
        confirmed_slot=None,
        calendar_event_link=None,
        last_interaction_call_sid="CA_cancel_1",
        updated_at="2026-04-20T12:00:00+00:00",
    )

    assert len(updated) == 1
    assert updated[0]["state"] == "cancelled"
    assert updated[0]["confirmed_slot"]["display"] == "Mon Apr 20 at 01:00 PM"
    assert updated[0]["calendar_event_link"] is None
    assert updated[0]["last_interaction_call_sid"] == "CA_cancel_1"


def test_derive_primary_booking_projection_handles_multiple_active_appointments():
    appointments = [
        {
            "event_id": "evt_morning",
            "state": "booked",
            "confirmed_slot": {"display": "Mon Apr 20 at 08:00 AM", "start": "2026-04-20T13:00:00Z"},
            "calendar_event_link": "https://calendar.google.com/event?eid=evt_morning",
        },
        {
            "event_id": "evt_afternoon",
            "state": "booked",
            "confirmed_slot": {"display": "Mon Apr 20 at 01:00 PM", "start": "2026-04-20T18:00:00Z"},
            "calendar_event_link": "https://calendar.google.com/event?eid=evt_afternoon",
        },
    ]

    slot, calendar_link = _derive_primary_booking_projection(appointments)

    assert slot["display"] == "2 active appointments"
    assert slot["active_count"] == 2
    assert slot["primary_display"] == "Mon Apr 20 at 08:00 AM"
    assert calendar_link is None


def test_sync_business_lead_after_booking_action_tracks_multiple_active_appointments(monkeypatch):
    monkeypatch.setattr(Config, "LEAD_BACKEND", "supabase")

    lead = {
        "id": "lead_1",
        "call_sid": "CA_existing",
        "issue_summary": "Customer requested office cleaning",
        "service_address": "Office 500",
        "metadata": {
            "appointments": [
                {
                    "event_id": "evt_existing",
                    "state": "booked",
                    "confirmed_slot": {"display": "Mon Apr 20 at 08:00 AM", "start": "2026-04-20T13:00:00Z"},
                    "calendar_event_link": "https://calendar.google.com/event?eid=evt_existing",
                    "summary": "Residential cleaning",
                    "service_address": "Home 123",
                }
            ]
        },
        "notes": [],
        "lead_status": "booked",
    }
    captured = {}

    monkeypatch.setattr(webhook_service_module, "get_lead_by_call_sid_sync", lambda call_sid: lead)
    monkeypatch.setattr(webhook_service_module, "find_related_lead_for_booking_sync", lambda event_id, contact_phone=None, limit=200: None)

    def _fake_update_lead_system_fields_by_id_sync(lead_id, updates):
        captured["lead_id"] = lead_id
        captured["updates"] = updates
        return True

    monkeypatch.setattr(
        webhook_service_module,
        "update_lead_system_fields_by_id_sync",
        _fake_update_lead_system_fields_by_id_sync,
    )

    lead_id = sync_business_lead_after_booking_action_sync(
        action="booked",
        call_sid="CA_existing",
        event_id="evt_new",
        contact_phone="+12185953061",
        confirmed_slot={"display": "Mon Apr 20 at 01:00 PM", "start": "2026-04-20T18:00:00Z"},
        calendar_event_link="https://calendar.google.com/event?eid=evt_new",
        appointment_summary="Office cleaning",
    )

    assert lead_id == "lead_1"
    assert captured["lead_id"] == "lead_1"
    assert captured["updates"]["lead_status"] == "booked"
    assert captured["updates"]["confirmed_slot"]["display"] == "2 active appointments"
    assert captured["updates"]["calendar_event_link"] is None
    assert "issue_summary" not in captured["updates"]
    assert "call_summary" not in captured["updates"]
    appointments = captured["updates"]["metadata"]["appointments"]
    assert len(appointments) == 2
    assert any(item["event_id"] == "evt_new" and item["summary"] == "Office cleaning" for item in appointments)
    assert captured["updates"]["metadata"]["last_booking_action"]["type"] == "booked"


def test_sync_business_lead_after_booking_action_follow_up_synthesizes_summaries(monkeypatch):
    monkeypatch.setattr(Config, "LEAD_BACKEND", "supabase")

    lead = {
        "id": "lead_followup_1",
        "call_sid": "CA_original",
        "lead_name": "Emeril",
        "issue_summary": "Move-out cleaning for a residential property.",
        "call_summary": "Original booking confirmed.",
        "service_address": "house 123 Street 7",
        "metadata": {
            "booking_event_id": "evt_moveout",
            "booking_state": "booked",
            "business_record_mode": "lifecycle",
            "primary_call_sid": "CA_original",
            "related_call_sids": ["CA_original"],
            "appointments": [
                {
                    "event_id": "evt_moveout",
                    "state": "booked",
                    "confirmed_slot": {"display": "Mon Apr 20 at 05:00 PM", "start": "2026-04-21T00:00:00Z"},
                    "calendar_event_link": "https://calendar.google.com/event?eid=evt_moveout",
                    "summary": "Move-out cleaning",
                    "service_address": "house 123 Street 7",
                }
            ],
        },
        "notes": [],
        "lead_status": "booked",
    }
    captured = {}

    monkeypatch.setattr(
        webhook_service_module,
        "find_related_lead_for_booking_sync",
        lambda event_id, contact_phone=None, limit=200: lead,
    )

    def _fake_update_lead_system_fields_by_id_sync(lead_id, updates):
        captured["lead_id"] = lead_id
        captured["updates"] = updates
        return True

    monkeypatch.setattr(
        webhook_service_module,
        "update_lead_system_fields_by_id_sync",
        _fake_update_lead_system_fields_by_id_sync,
    )

    lead_id = sync_business_lead_after_booking_action_sync(
        action="rescheduled",
        call_sid="CA_followup",
        event_id="evt_moveout",
        contact_phone="+12185953061",
        confirmed_slot={"display": "Mon Apr 20 at 09:00 PM", "start": "2026-04-21T04:00:00Z"},
        note="Caller requested reschedule. Appointment moved to Mon Apr 20 at 09:00 PM.",
    )

    assert lead_id == "lead_followup_1"
    assert captured["updates"]["call_sid"] == "CA_followup"
    assert captured["updates"]["issue_summary"] == (
        "Move-out cleaning at house 123 Street 7; appointment rescheduled from Mon Apr 20 at 05:00 PM to Mon Apr 20 at 09:00 PM."
    )
    assert captured["updates"]["call_summary"] == (
        "Emeril called to reschedule Move-out cleaning at house 123 Street 7 from Mon Apr 20 at 05:00 PM to Mon Apr 20 at 09:00 PM."
    )
    assert captured["updates"]["metadata"]["last_booking_action"]["type"] == "rescheduled"
    assert captured["updates"]["metadata"]["last_booking_action"]["call_sid"] == "CA_followup"
    assert captured["updates"]["metadata"]["last_booking_action"]["event_id"] == "evt_moveout"
    assert captured["updates"]["metadata"]["last_booking_action"]["previous_confirmed_slot"]["display"] == "Mon Apr 20 at 05:00 PM"
    assert captured["updates"]["metadata"]["last_booking_action"]["current_confirmed_slot"]["display"] == "Mon Apr 20 at 09:00 PM"
    assert captured["updates"]["metadata"]["related_call_sids"] == ["CA_original", "CA_followup"]
    assert captured["updates"]["metadata"]["primary_call_sid"] == "CA_original"


def test_update_business_lead_from_payload_sync_keeps_backend_synthesized_follow_up_summaries(monkeypatch):
    monkeypatch.setattr(Config, "LEAD_BACKEND", "supabase")

    lead = {
        "id": "lead_business_42",
        "call_sid": "CA_followup",
        "lead_name": "Emeril",
        "service_address": "house 123 Street 7",
        "metadata": {
            "business_record_mode": "lifecycle",
            "primary_call_sid": "CA_original",
            "related_call_sids": ["CA_original", "CA_followup"],
            "last_booking_action": {
                "type": "cancelled",
                "event_id": "evt_moveout",
                "call_sid": "CA_followup",
                "timestamp": "2026-04-20T10:16:33+00:00",
                "previous_confirmed_slot": {"display": "Mon Apr 20 at 09:00 PM"},
                "current_confirmed_slot": None,
                "summary": "Move-out cleaning",
                "service_address": "house 123 Street 7",
            },
            "appointments": [
                {
                    "event_id": "evt_moveout",
                    "state": "cancelled",
                    "confirmed_slot": {"display": "Mon Apr 20 at 09:00 PM"},
                    "summary": "Move-out cleaning",
                    "service_address": "house 123 Street 7",
                }
            ],
        },
    }
    captured = {}

    monkeypatch.setattr(webhook_service_module, "get_lead_by_id_sync", lambda lead_id: lead)

    def _fake_update_lead_system_fields_by_id_sync(lead_id, updates):
        captured["lead_id"] = lead_id
        captured["updates"] = updates
        return True

    monkeypatch.setattr(
        webhook_service_module,
        "update_lead_system_fields_by_id_sync",
        _fake_update_lead_system_fields_by_id_sync,
    )

    ok = update_business_lead_from_payload_sync(
        "lead_business_42",
        {
            "company_name": "Prestige Cleaning Services",
            "industry": "cleaning",
            "priority": "routine",
            "contact": {
                "name": "Emeril",
                "phone": "+12185953061",
                "email": "",
            },
            "issue_summary": "Cancellation request",
            "call_summary": "Caller requested cancellation.",
            "call_sid": "CA_followup",
            "timestamp": "2026-04-20T10:16:35+00:00",
        },
    )

    assert ok is True
    assert captured["lead_id"] == "lead_business_42"
    assert captured["updates"]["issue_summary"] == (
        "Move-out cleaning at house 123 Street 7; appointment cancelled after being scheduled for Mon Apr 20 at 09:00 PM."
    )
    assert captured["updates"]["call_summary"] == (
        "Emeril called to cancel Move-out cleaning at house 123 Street 7, which had been scheduled for Mon Apr 20 at 09:00 PM."
    )
