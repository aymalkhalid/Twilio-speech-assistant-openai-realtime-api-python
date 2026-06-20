from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_uses_calls_api_and_valid_call_record_identifiers():
    html = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    forbidden = [
        "loadCall Records",
        "buildCall Records",
        "newCall Records",
        "refetchCall Records",
        "startCall Records",
        "stopCall Records",
        "scheduleCall Records",
    ]
    assert not any(token in html for token in forbidden)
    assert '"/calls?"' in html
    assert '"/calls/events"' in html
    assert '"/leads?"' not in html
    assert '"/leads/events"' not in html
    assert "call_records_changed" in html


def test_call_record_facade_is_primary_route_surface():
    main_py = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/calls"' in main_py
    assert '@app.patch("/calls/{record_id}"' in main_py
    assert '@app.delete("/calls/{record_id}"' in main_py
    assert '@app.get("/leads"' not in main_py
    assert 'from services.call_records_service import' in main_py
    assert 'from services.lead_events import' not in main_py
