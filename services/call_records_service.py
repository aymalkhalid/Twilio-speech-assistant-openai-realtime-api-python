"""
Generic call-record facade.

The starter exposes call records as the application concept. The current Supabase
schema still uses the original leads table/columns for compatibility, so this module
keeps that mapping behind one adapter boundary.
"""

from __future__ import annotations

from typing import Any

from config import Config
from services import webhook_service as _legacy_storage


CallRecordUpdateSchemaError = _legacy_storage.LeadUpdateSchemaError
CALL_RECORD_BACKEND_NAMES = _legacy_storage.LEAD_BACKEND_NAMES


def _effective_backend() -> str:
    call_backend = str(getattr(Config, "CALL_RECORD_BACKEND", "") or "").strip().lower()
    legacy_backend = str(getattr(Config, "LEAD_BACKEND", "") or "").strip().lower()
    if legacy_backend and legacy_backend != call_backend and legacy_backend != "webhook":
        return legacy_backend
    return call_backend or legacy_backend or "webhook"


def _sync_legacy_config_aliases() -> None:
    backend = _effective_backend()
    Config.CALL_RECORD_BACKEND = backend
    Config.LEAD_BACKEND = backend
    table = (
        str(getattr(Config, "SUPABASE_CALL_RECORD_TABLE", "") or "").strip()
        or str(getattr(Config, "SUPABASE_LEAD_TABLE", "") or "").strip()
        or "leads"
    )
    Config.SUPABASE_CALL_RECORD_TABLE = table
    Config.SUPABASE_LEAD_TABLE = table


def has_call_record_backend_configured() -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.has_lead_backend_configured()


def build_call_record_payload(**kwargs: Any) -> dict[str, Any]:
    _sync_legacy_config_aliases()
    return _legacy_storage.build_handoff_payload(**kwargs)


def call_record_payload_to_supabase_row(payload: dict[str, Any]) -> dict[str, Any]:
    _sync_legacy_config_aliases()
    return _legacy_storage.handoff_payload_to_supabase_row(payload)


def call_record_payload_to_supabase_updates(payload: dict[str, Any]) -> dict[str, Any]:
    _sync_legacy_config_aliases()
    return _legacy_storage.handoff_payload_to_supabase_updates(payload)


def list_call_records_sync(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
    _sync_legacy_config_aliases()
    return _legacy_storage.list_leads_sync(**kwargs)


def get_call_record_by_id_sync(record_id: str | None) -> dict[str, Any] | None:
    _sync_legacy_config_aliases()
    return _legacy_storage.get_lead_by_id_sync(record_id)


def get_call_record_by_call_sid_sync(call_sid: str | None) -> dict[str, Any] | None:
    _sync_legacy_config_aliases()
    return _legacy_storage.get_lead_by_call_sid_sync(call_sid)


def update_call_record_by_id_sync(record_id: str | None, updates: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.update_lead_by_id_sync(record_id, updates)


def update_call_record_system_fields_by_id_sync(record_id: str | None, updates: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.update_lead_system_fields_by_id_sync(record_id, updates)


async def update_call_record_system_fields_by_id_async(record_id: str | None, updates: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return await _legacy_storage.update_lead_system_fields_by_id_async(record_id, updates)


def update_call_record_by_call_sid_sync(call_sid: str | None, updates: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.update_lead_by_call_sid_sync(call_sid, updates)


async def update_call_record_by_call_sid_async(call_sid: str | None, updates: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return await _legacy_storage.update_lead_by_call_sid_async(call_sid, updates)


def delete_call_record_by_id_sync(record_id: str | None) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.delete_lead_by_id_sync(record_id)


def append_call_record_note_by_call_sid_sync(call_sid: str | None, note: str) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.append_lead_note_by_call_sid_sync(call_sid, note)


async def append_call_record_note_by_call_sid_async(call_sid: str | None, note: str) -> bool:
    _sync_legacy_config_aliases()
    return await _legacy_storage.append_lead_note_by_call_sid_async(call_sid, note)


def find_related_call_record_for_booking_sync(**kwargs: Any) -> dict[str, Any] | None:
    _sync_legacy_config_aliases()
    return _legacy_storage.find_related_lead_for_booking_sync(**kwargs)


def sync_call_record_after_booking_action_sync(**kwargs: Any) -> str | None:
    _sync_legacy_config_aliases()
    return _legacy_storage.sync_business_lead_after_booking_action_sync(**kwargs)


async def sync_call_record_after_booking_action_async(**kwargs: Any) -> str | None:
    _sync_legacy_config_aliases()
    return await _legacy_storage.sync_business_lead_after_booking_action_async(**kwargs)


def update_existing_call_record_from_payload_sync(record_id: str | None, payload: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.update_business_lead_from_payload_sync(record_id, payload)


async def update_existing_call_record_from_payload_async(record_id: str | None, payload: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return await _legacy_storage.update_business_lead_from_payload_async(record_id, payload)


def save_call_record_sync(payload: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return _legacy_storage.send_handoff_sync(payload)


async def save_call_record_async(payload: dict[str, Any]) -> bool:
    _sync_legacy_config_aliases()
    return await _legacy_storage.deliver_lead_async(payload)


# Legacy aliases kept for older tests/extensions that still import the old names.
LeadUpdateSchemaError = CallRecordUpdateSchemaError
LEAD_BACKEND_NAMES = CALL_RECORD_BACKEND_NAMES
has_lead_backend_configured = has_call_record_backend_configured
build_handoff_payload = build_call_record_payload
handoff_payload_to_supabase_row = call_record_payload_to_supabase_row
handoff_payload_to_supabase_updates = call_record_payload_to_supabase_updates
list_leads_sync = list_call_records_sync
get_lead_by_id_sync = get_call_record_by_id_sync
get_lead_by_call_sid_sync = get_call_record_by_call_sid_sync
update_lead_by_id_sync = update_call_record_by_id_sync
update_lead_system_fields_by_id_sync = update_call_record_system_fields_by_id_sync
update_lead_system_fields_by_id_async = update_call_record_system_fields_by_id_async
update_lead_by_call_sid_sync = update_call_record_by_call_sid_sync
update_lead_by_call_sid_async = update_call_record_by_call_sid_async
delete_lead_by_id_sync = delete_call_record_by_id_sync
append_lead_note_by_call_sid_sync = append_call_record_note_by_call_sid_sync
append_lead_note_by_call_sid_async = append_call_record_note_by_call_sid_async
find_related_lead_for_booking_sync = find_related_call_record_for_booking_sync
sync_business_lead_after_booking_action_sync = sync_call_record_after_booking_action_sync
sync_business_lead_after_booking_action_async = sync_call_record_after_booking_action_async
update_business_lead_from_payload_sync = update_existing_call_record_from_payload_sync
update_business_lead_from_payload_async = update_existing_call_record_from_payload_async
send_handoff_sync = save_call_record_sync
deliver_lead_async = save_call_record_async
send_handoff_async = save_call_record_async
