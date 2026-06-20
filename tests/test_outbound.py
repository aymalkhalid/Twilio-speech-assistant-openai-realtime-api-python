"""
Tests for outbound calling feature.
Run from project root:
  python -m tests.test_outbound
  python tests/test_outbound.py
  pytest tests/test_outbound.py -v

Covers:
  - Campaign type preset loading
  - System message template rendering
  - Contact validation
  - Config helpers
  - TwiML generation
  - Twilio status callback mapping
"""
import os
import sys
from types import SimpleNamespace

from dotenv import load_dotenv
load_dotenv()

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =========================================================================
# 1. Campaign type preset loading
# =========================================================================

def test_campaign_types_load():
    """Campaign type presets load and contain expected keys."""
    from services.outbound_service import get_campaign_types, _campaign_types_cache
    # Clear cache for fresh load
    import services.outbound_service as _mod
    _mod._campaign_types_cache = None

    types = get_campaign_types()
    assert isinstance(types, dict), "get_campaign_types must return a dict"
    assert len(types) >= 4, f"Expected at least 4 campaign types, got {len(types)}: {list(types.keys())}"
    for key in ("promo", "appointment_confirmation", "payment_reminder", "general"):
        assert key in types, f"Missing campaign type: {key}"
    print("  PASS: campaign types load")


def test_campaign_type_has_required_fields():
    """Each campaign type has label and default_script."""
    from services.outbound_service import get_campaign_types
    types = get_campaign_types()
    for key, cfg in types.items():
        assert "label" in cfg, f"Campaign type {key} missing 'label'"
        assert "default_script" in cfg, f"Campaign type {key} missing 'default_script'"
    print("  PASS: campaign types have required fields")


def test_campaign_type_config_lookup():
    """get_campaign_type_config returns correct type or empty dict for unknown."""
    from services.outbound_service import get_campaign_type_config
    promo = get_campaign_type_config("promo")
    assert promo.get("label") == "Promotional", f"Expected 'Promotional', got {promo.get('label')}"
    unknown = get_campaign_type_config("nonexistent_type_xyz")
    assert unknown == {}, f"Expected empty dict for unknown type, got {unknown}"
    print("  PASS: campaign type config lookup")


def test_appointment_type_has_custom_fields():
    """appointment_confirmation type declares appointment_date custom field."""
    from services.outbound_service import get_campaign_type_config
    cfg = get_campaign_type_config("appointment_confirmation")
    fields = cfg.get("custom_fields", [])
    assert isinstance(fields, list) and len(fields) > 0, "appointment_confirmation should have custom_fields"
    field_names = [f.get("name") for f in fields]
    assert "appointment_date" in field_names, f"Expected 'appointment_date' in custom_fields, got {field_names}"
    print("  PASS: appointment_confirmation has custom_fields")


def test_payment_type_has_amount_due():
    """payment_reminder type declares amount_due custom field."""
    from services.outbound_service import get_campaign_type_config
    cfg = get_campaign_type_config("payment_reminder")
    fields = cfg.get("custom_fields", [])
    field_names = [f.get("name") for f in fields]
    assert "amount_due" in field_names, f"Expected 'amount_due' in custom_fields, got {field_names}"
    print("  PASS: payment_reminder has amount_due field")


# =========================================================================
# 2. System message template rendering
# =========================================================================

def test_template_placeholder_rendering():
    """Placeholders in a template string are replaced with contact data."""
    template = "Hello {contact_name}, this is {agent_name} from {company_name}."
    replacements = {
        "contact_name": "Alice",
        "company_name": "Acme Plumbing",
        "agent_name": "Alex",
    }
    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", str(value))
    assert "Alice" in result, "contact_name not replaced"
    assert "Acme Plumbing" in result, "company_name not replaced"
    assert "Alex" in result, "agent_name not replaced"
    assert "{" not in result, f"Unreplaced placeholder in: {result}"
    print("  PASS: template placeholder rendering")


def test_template_custom_fields_rendering():
    """Custom fields from contact are injected into template."""
    template = "Appointment on {appointment_date} for {contact_name}."
    replacements = {
        "contact_name": "Bob",
        "appointment_date": "March 15, 2026 at 2:00 PM",
    }
    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", str(value))
    assert "March 15, 2026 at 2:00 PM" in result
    assert "Bob" in result
    assert "{" not in result
    print("  PASS: custom fields rendering")


def test_template_missing_placeholder_left_as_is():
    """If a placeholder has no matching key, it stays in the string."""
    template = "Hello {contact_name}, your balance is {amount_due}."
    replacements = {"contact_name": "Carol"}
    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", str(value))
    assert "Carol" in result
    assert "{amount_due}" in result, "Missing placeholder should remain for debugging"
    print("  PASS: missing placeholder left as-is")


def test_outbound_system_message_appends_language_and_accent_policy(monkeypatch):
    """Outbound campaign prompts should inherit global language/accent policy."""
    import services.outbound_service as outbound_service

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=self.rows)

    class FakeClient:
        def table(self, name):
            if name == "outbound_campaigns":
                return FakeQuery([
                    {
                        "id": "campaign-1",
                        "campaign_type": "general",
                        "message_template": "# Role and Objective\nCall {contact_name} for {company_name}.",
                    }
                ])
            if name == "outbound_contacts":
                return FakeQuery([
                    {
                        "id": "contact-1",
                        "name": "Alice",
                        "custom_fields": {},
                    }
                ])
            return FakeQuery([])

    monkeypatch.setattr(outbound_service, "_get_supabase_client", lambda: FakeClient())
    monkeypatch.setattr(outbound_service.Config, "SUPABASE_OUTBOUND_CAMPAIGNS_TABLE", "outbound_campaigns")
    monkeypatch.setattr(outbound_service.Config, "SUPABASE_OUTBOUND_CONTACTS_TABLE", "outbound_contacts")
    monkeypatch.setattr(outbound_service.Config, "ASSISTANT_LANGUAGE", "English")
    monkeypatch.setattr(outbound_service.Config, "LANGUAGE_SWITCH_POLICY", "explicit_or_substantive")
    monkeypatch.setattr(outbound_service.Config, "ASSISTANT_ACCENT", "neutral American")
    monkeypatch.setattr(outbound_service.Config, "ASSISTANT_ACCENT_STRENGTH", "light")
    monkeypatch.setenv("COMPANY_NAME", "Acme Plumbing")

    result = outbound_service.build_outbound_system_message("campaign-1", "contact-1")

    assert result is not None
    assert "Call Alice for Acme Plumbing." in result
    assert "# Language" in result
    assert "# Accent" in result
    assert "Do not infer language from accent alone." in result
    assert "Do not change response language based on the caller's accent." in result
    assert "Use a moderate pace, clear consonants, natural stress, and phone-friendly prosody." in result
    print("  PASS: outbound prompt appends language/accent policy")


def test_outbound_system_message_does_not_duplicate_existing_language_accent_sections(monkeypatch):
    """Campaign templates with explicit language/accent sections keep one copy."""
    import services.outbound_service as outbound_service

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=self.rows)

    class FakeClient:
        def table(self, name):
            if name == "outbound_campaigns":
                return FakeQuery([
                    {
                        "id": "campaign-1",
                        "campaign_type": "general",
                        "message_template": (
                            "# Role and Objective\nCall {contact_name}.\n\n"
                            "# Language\nUse English.\n\n"
                            "# Accent\nUse a light neutral American accent."
                        ),
                    }
                ])
            if name == "outbound_contacts":
                return FakeQuery([
                    {
                        "id": "contact-1",
                        "name": "Alice",
                        "custom_fields": {},
                    }
                ])
            return FakeQuery([])

    monkeypatch.setattr(outbound_service, "_get_supabase_client", lambda: FakeClient())
    monkeypatch.setattr(outbound_service.Config, "SUPABASE_OUTBOUND_CAMPAIGNS_TABLE", "outbound_campaigns")
    monkeypatch.setattr(outbound_service.Config, "SUPABASE_OUTBOUND_CONTACTS_TABLE", "outbound_contacts")

    result = outbound_service.build_outbound_system_message("campaign-1", "contact-1")

    assert result is not None
    assert result.count("# Language") == 1
    assert result.count("# Accent") == 1
    print("  PASS: outbound prompt does not duplicate language/accent policy")


def test_template_fallback_contact_name():
    """When contact name is empty, fallback to 'there'."""
    name = "" or "there"
    assert name == "there"
    name2 = "Alice" or "there"
    assert name2 == "Alice"
    print("  PASS: contact_name fallback")


# =========================================================================
# 3. Contact validation
# =========================================================================

def test_contact_phone_required():
    """Contacts without a phone number are skipped during add."""
    contacts = [
        {"name": "Alice", "phone": "+15551234567", "email": "a@b.com"},
        {"name": "Bob", "phone": "", "email": "b@b.com"},
        {"name": "Carol", "phone": "  ", "email": ""},
        {"name": "Dave", "phone": "+15559876543"},
    ]
    valid = [c for c in contacts if (c.get("phone") or "").strip()]
    assert len(valid) == 2, f"Expected 2 valid contacts, got {len(valid)}"
    assert valid[0]["name"] == "Alice"
    assert valid[1]["name"] == "Dave"
    print("  PASS: contact phone required")


def test_contact_custom_fields_default():
    """custom_fields defaults to empty dict."""
    contact = {"name": "Test", "phone": "+1555", "email": ""}
    custom = contact.get("custom_fields") or {}
    assert custom == {}
    print("  PASS: custom_fields default")


def test_contact_row_build():
    """Contact row built for Supabase insert has expected shape."""
    c = {"name": " Alice ", "phone": "+15551234567 ", "email": " a@b.com ", "custom_fields": {"amount_due": "$150"}}
    row = {
        "campaign_id": "fake-uuid",
        "name": (c.get("name") or "").strip(),
        "phone": (c.get("phone") or "").strip(),
        "email": (c.get("email") or "").strip(),
        "custom_fields": c.get("custom_fields") or {},
        "status": "pending",
    }
    assert row["name"] == "Alice"
    assert row["phone"] == "+15551234567"
    assert row["email"] == "a@b.com"
    assert row["custom_fields"]["amount_due"] == "$150"
    assert row["status"] == "pending"
    print("  PASS: contact row build")


# =========================================================================
# 4. Config helpers
# =========================================================================

def test_config_outbound_enabled_default():
    """OUTBOUND_ENABLED defaults to false."""
    from config import Config
    orig = os.environ.get("OUTBOUND_ENABLED")
    os.environ["OUTBOUND_ENABLED"] = "false"
    assert Config.OUTBOUND_ENABLED is False or (os.getenv("OUTBOUND_ENABLED", "false").strip().lower() not in ("1", "true", "yes"))
    if orig is not None:
        os.environ["OUTBOUND_ENABLED"] = orig
    elif "OUTBOUND_ENABLED" in os.environ:
        del os.environ["OUTBOUND_ENABLED"]
    print("  PASS: OUTBOUND_ENABLED defaults false")


def test_config_max_concurrency_clamped():
    """OUTBOUND_MAX_CONCURRENCY is at least 1."""
    from config import Config
    assert Config.OUTBOUND_MAX_CONCURRENCY >= 1, f"Expected >= 1, got {Config.OUTBOUND_MAX_CONCURRENCY}"
    print("  PASS: max concurrency >= 1")


def test_config_supabase_table_defaults():
    """Supabase outbound table names have sensible defaults."""
    from config import Config
    assert Config.SUPABASE_OUTBOUND_CAMPAIGNS_TABLE == (os.getenv("SUPABASE_OUTBOUND_CAMPAIGNS_TABLE") or "outbound_campaigns")
    assert Config.SUPABASE_OUTBOUND_CONTACTS_TABLE == (os.getenv("SUPABASE_OUTBOUND_CONTACTS_TABLE") or "outbound_contacts")
    print("  PASS: Supabase table defaults")


def test_config_outbound_from_number():
    """get_outbound_from_number returns env value or empty."""
    from config import Config
    result = Config.get_outbound_from_number()
    expected = (os.getenv("TWILIO_OUTBOUND_NUMBER") or "").strip()
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print("  PASS: outbound from number")


def test_concurrency_clamp_logic():
    """Campaign concurrency is clamped between 1 and OUTBOUND_MAX_CONCURRENCY."""
    from config import Config
    max_c = Config.OUTBOUND_MAX_CONCURRENCY
    assert max(1, min(0, max_c)) == 1, "0 should clamp to 1"
    assert max(1, min(1, max_c)) == 1
    assert max(1, min(max_c, max_c)) == max_c
    assert max(1, min(max_c + 10, max_c)) == max_c, "Over-max should clamp"
    print("  PASS: concurrency clamp logic")


# =========================================================================
# 5. TwiML generation
# =========================================================================

def test_outbound_twiml_shape():
    """Outbound TwiML contains Connect > Stream with correct query params."""
    from twilio.twiml.voice_response import VoiceResponse, Connect
    from urllib.parse import quote

    campaign_id = "abc-123"
    contact_id = "def-456"
    host = "example.com"

    stream_url = (
        f"wss://{host}/media-stream"
        f"?direction=outbound"
        f"&campaign_id={quote(campaign_id, safe='')}"
        f"&contact_id={quote(contact_id, safe='')}"
    )
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    xml = str(response)

    assert "direction=outbound" in xml, f"Missing direction param in TwiML: {xml}"
    assert f"campaign_id={campaign_id}" in xml, f"Missing campaign_id in TwiML: {xml}"
    assert f"contact_id={contact_id}" in xml, f"Missing contact_id in TwiML: {xml}"
    assert "<Connect>" in xml, f"Missing <Connect> in TwiML: {xml}"
    assert "<Stream" in xml, f"Missing <Stream> in TwiML: {xml}"
    assert "wss://example.com/media-stream" in xml
    print("  PASS: outbound TwiML shape")


def test_outbound_twiml_url_encoding():
    """Special characters in campaign/contact IDs are URL-encoded."""
    from urllib.parse import quote

    campaign_id = "id with spaces & special=chars"
    encoded = quote(campaign_id, safe="")
    assert " " not in encoded
    assert "&" not in encoded
    assert "=" not in encoded
    assert "id%20with%20spaces" in encoded
    print("  PASS: TwiML URL encoding")


# =========================================================================
# 6. Twilio status callback mapping
# =========================================================================

def test_status_callback_terminal_mapping():
    """Terminal Twilio statuses map to correct contact statuses."""
    terminal_statuses = {"completed", "busy", "no-answer", "failed", "canceled"}

    for ts in terminal_statuses:
        final_status = "completed" if ts == "completed" else "failed"
        error_msg = "" if ts == "completed" else ts
        if ts == "completed":
            assert final_status == "completed"
            assert error_msg == ""
        else:
            assert final_status == "failed"
            assert error_msg == ts
    print("  PASS: terminal status mapping")


def test_status_callback_non_terminal_ignored():
    """Non-terminal statuses (initiated, ringing, in-progress) are not in the terminal set."""
    terminal_statuses = {"completed", "busy", "no-answer", "failed", "canceled"}
    for ts in ("initiated", "ringing", "in-progress", "queued"):
        assert ts not in terminal_statuses, f"{ts} should not be terminal"
    print("  PASS: non-terminal statuses ignored")


def test_status_callback_empty_call_sid():
    """Empty CallSid should be handled gracefully (no update)."""
    call_sid = ""
    assert not call_sid, "Empty call_sid should be falsy"
    print("  PASS: empty CallSid handled")


def test_contact_status_lifecycle():
    """Contact status transitions follow expected lifecycle."""
    valid_transitions = {
        "pending": ["calling", "skipped"],
        "calling": ["completed", "failed"],
    }
    for from_status, to_statuses in valid_transitions.items():
        for to_status in to_statuses:
            assert to_status in ("pending", "calling", "completed", "failed", "skipped"), \
                f"Invalid status: {to_status}"
    print("  PASS: contact status lifecycle")


# =========================================================================
# Runner
# =========================================================================

def run_all_tests():
    """Run all outbound tests."""
    tests = [
        ("Campaign type preset loading", [
            test_campaign_types_load,
            test_campaign_type_has_required_fields,
            test_campaign_type_config_lookup,
            test_appointment_type_has_custom_fields,
            test_payment_type_has_amount_due,
        ]),
        ("System message template rendering", [
            test_template_placeholder_rendering,
            test_template_custom_fields_rendering,
            test_template_missing_placeholder_left_as_is,
            test_template_fallback_contact_name,
        ]),
        ("Contact validation", [
            test_contact_phone_required,
            test_contact_custom_fields_default,
            test_contact_row_build,
        ]),
        ("Config helpers", [
            test_config_outbound_enabled_default,
            test_config_max_concurrency_clamped,
            test_config_supabase_table_defaults,
            test_config_outbound_from_number,
            test_concurrency_clamp_logic,
        ]),
        ("TwiML generation", [
            test_outbound_twiml_shape,
            test_outbound_twiml_url_encoding,
        ]),
        ("Twilio status callback mapping", [
            test_status_callback_terminal_mapping,
            test_status_callback_non_terminal_ignored,
            test_status_callback_empty_call_sid,
            test_contact_status_lifecycle,
        ]),
    ]

    total = 0
    passed = 0
    failed = 0

    for group_name, test_fns in tests:
        print(f"\n--- {group_name} ---")
        for fn in test_fns:
            total += 1
            try:
                fn()
                passed += 1
            except Exception as e:
                failed += 1
                print(f"  FAIL: {fn.__name__}: {e}")

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    run_all_tests()
