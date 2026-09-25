"""Chat repairs exercised with synthetic responses and no model processes."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from fastapi import HTTPException
from chat_sessions import ChatMessage, ChatSessionStore, render_history_for_worker
from routers import workers, chat_sessions_routes as sessions
from worker_registry import WorkerInfo, WorkerRegistry
from snapshot_install import inspect_snapshot


class ChatRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_tools_never_reserve_a_worker(self):
        for kwargs in ({"tools": [{"function": "bad"}]},
                       {"tools": [{"function": {"name": "x"}}], "tool_choice": {"function": "bad"}},
                       {"response_format": {"type": "json_schema", "json_schema": "bad"}}):
            request = workers.ChatRequest(model="qwen_omni_3b", **kwargs)
            with patch.object(workers, "_resolve_busy_worker", new_callable=AsyncMock) as resolve:
                with self.assertRaises(HTTPException) as caught:
                    await workers.chat(request.model, request)
                self.assertEqual(caught.exception.status_code, 422)
                resolve.assert_not_awaited()

    async def test_preamble_limit_checked_before_reservation(self):
        request = workers.ChatRequest(model="qwen_omni_3b", text="a" * workers.INFER_MAX_TEXT_CHARS,
                                      response_format={"type": "json_object"})
        with patch.object(workers, "_resolve_busy_worker", new_callable=AsyncMock) as resolve:
            with self.assertRaises(HTTPException):
                await workers.chat(request.model, request)
            resolve.assert_not_awaited()

    async def test_unstarted_response_does_not_reserve_worker(self):
        request = workers.ChatRequest(model="qwen_omni_3b", text="hello")
        with patch.object(workers, "_resolve_busy_worker", new_callable=AsyncMock) as resolve:
            response = await workers.chat_stream(request.model, request)
            await response.body_iterator.aclose()
            resolve.assert_not_awaited()

    async def test_disconnect_closes_nested_stream_and_aborts_owner(self):
        request = workers.ChatRequest(model="qwen_omni_3b", text="hello")
        registry = WorkerRegistry()
        worker = WorkerInfo("w-test", request.model, 8299, "cpu", status="ready")
        registry.register(worker)
        with patch.object(workers, "worker_registry", registry), patch.object(workers, "_abort_stream_worker", new_callable=AsyncMock) as abort:
            response = await workers.chat_stream(request.model, request)
            event = json.loads((await anext(response.body_iterator)).decode()[5:])
            self.assertEqual(worker.status, "busy")
            await response.body_iterator.aclose()
            abort.assert_awaited_once_with(worker, event["job_id"])
            self.assertEqual(worker.status, "ready")

    async def test_busy_autospawn_does_not_duplicate_model(self):
        registry = WorkerRegistry()
        registry.register(WorkerInfo("w-test", "qwen_omni_3b", 8299, "cpu", status="busy"))
        with patch.object(workers, "worker_registry", registry), patch.object(workers.worker_manager, "spawn_worker", new_callable=AsyncMock) as spawn:
            with self.assertRaises(HTTPException):
                await workers._resolve_busy_worker("qwen_omni_3b", autospawn=True)
            spawn.assert_not_awaited()

    def test_latest_image_is_selected(self):
        req = workers._OAIChatRequest(model="qwen_omni_3b", messages=[
            {"role": "user", "content": [{"type": "image_url", "image_url": "data:image/png;base64,old"}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": "data:image/png;base64,new"}]},
        ])
        self.assertEqual(workers._make_oai_chat_payload(req).image, "new")

    def test_structured_output_parser_preserves_tools(self):
        req = workers.ChatRequest(model="qwen_omni_3b", tools=[{"function": {"name": "test"}}])
        result = workers._format_generated_text('<tool_call>{"name":"test","arguments":{"x":1}}</tool_call>', req)
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"x": 1})


class SessionRepairTests(unittest.TestCase):
    def test_every_access_expires_idle_sessions(self):
        for access in ("get", "snapshot", "append_message"):
            store = ChatSessionStore(ttl_seconds=10)
            with patch("chat_sessions.time.time", return_value=100):
                session = store.create("qwen_omni_3b")
            with patch("chat_sessions.time.time", return_value=session.last_activity + 11):
                method = getattr(store, access)
                args = [session.session_id] + ([ChatMessage("user", "late")] if access == "append_message" else [])
                self.assertIsNone(method(*args))

    def test_turns_serialize_and_active_session_survives_ttl(self):
        store = ChatSessionStore(ttl_seconds=10)
        session = store.create("qwen_omni_3b")
        store.begin_turn(session.session_id)
        with self.assertRaises(ValueError):
            store.begin_turn(session.session_id)
        with patch("chat_sessions.time.time", return_value=session.last_activity + 20):
            self.assertIsNotNone(store.get(session.session_id))
            store.end_turn(session.session_id)
            self.assertIsNone(store.get(session.session_id))

    def test_prompt_trims_oldest_history_and_keeps_system_and_current(self):
        store = ChatSessionStore()
        session = store.create("qwen_omni_3b", system="system")
        store.append_message(session.session_id, ChatMessage("user", "old" * 100))
        session = store.get(session.session_id)
        result = render_history_for_worker(session, ChatMessage("user", "new"), max_chars=40)
        self.assertEqual(result, "system\nUser: new\nAssistant:")
        with self.assertRaises(ValueError):
            render_history_for_worker(session, ChatMessage("user", "new" * 100), max_chars=40)

    def test_session_reuses_latest_media_and_reserves_json_preamble(self):
        store = ChatSessionStore()
        session = store.create("qwen_omni_3b")
        store.append_message(session.session_id, ChatMessage("user", "image", image="old-image"))
        req = sessions.SessionMessageRequest(content="describe it", response_format={"type": "json_object"})
        _, native, effective = sessions._session_request(store.get(session.session_id), req)
        self.assertEqual(native.image, "old-image")
        self.assertIn("# Response format", effective.text)
        self.assertIn("describe it", effective.text)

    def test_media_store_budget_rejects_excess_active_history(self):
        store = ChatSessionStore()
        session = store.create("qwen_omni_3b")
        store.begin_turn(session.session_id)
        with patch("chat_sessions.OMNI_CHAT_STORE_MAX_BYTES", 4):
            with self.assertRaises(ValueError):
                store.append_message(session.session_id, ChatMessage("user", "large"))
        self.assertEqual(store.get(session.session_id).messages, [])

    def test_snapshot_rejects_external_declared_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "model"
            root.mkdir()
            (root / "weight.bin").write_bytes(b"x")
            outside = Path(tmp) / "outside.bin"
            outside.write_bytes(b"x")
            index = root / "model.index.json"
            for relative in ("../outside.bin", str(outside)):
                index.write_text(json.dumps({"weight_map": {"layer": relative}}))
                self.assertFalse(inspect_snapshot(root)["valid"])


if __name__ == "__main__":
    unittest.main()
