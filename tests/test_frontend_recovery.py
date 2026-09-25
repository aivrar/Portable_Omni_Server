from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_reacquires_session_and_schedules_bounded_retry():
    source = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    assert "resp.status === 401" in source
    assert "initSession({ silent: true })" in source
    assert "_reconnectTimer" in source
    assert "Math.min(1000 * (1 << (this._backoff - 1)), 8000)" in source


def test_bridge_serves_self_reloading_page_while_gateway_starts():
    source = (ROOT / "bridge.py").read_text(encoding="utf-8")
    assert "Starting Omni Studio" in source
    assert "location.reload()" in source
    assert 'self.send_response(503)' in source
