import asyncio
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import sessions  # noqa: E402


def test_open_url_hands_blocking_os_work_to_a_thread():
    calls = []

    async def fake_to_thread(fn, *args):
        calls.append((fn, args))
        return True, "test-browser"

    request = sessions.OpenUrlRequest(url="https://example.com", browser="default")
    with mock.patch.object(sessions.asyncio, "to_thread", side_effect=fake_to_thread):
        result = asyncio.run(sessions.open_url(request))

    assert result == {"opened": True, "method": "test-browser"}
    assert calls == [(sessions._open_system_browser, ("https://example.com", "default"))]


def test_session_advertises_full_app_shutdown():
    source = Path(sessions.__file__).read_text(encoding="utf-8")
    assert '"POST /api/app/shutdown"' in source
