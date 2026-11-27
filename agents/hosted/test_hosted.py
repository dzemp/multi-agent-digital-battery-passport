"""Unit tests for the hosted agent source. They need the packages in requirements.txt and make no Azure calls.

These packages conflict with the app's, so use a separate environment (see CONTRIBUTING.md).
Run from battery-passport-mvp:  python -m unittest discover -s agents/hosted
"""

import asyncio
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "app"))
ENDPOINT = "https://unit.services.ai.azure.com/api/projects/passport"
ENV = {"AGENT_ROLE": "supplier", "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT, "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-6.1-sol"}

try:
    import agent_framework_foundry_hosting  # noqa: F401
    import azure.ai.agentserver.invocations  # noqa: F401
    with patch.dict(os.environ, ENV):
        spec = importlib.util.spec_from_file_location("hosted_agent", HERE / "main.py")
        hosted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hosted)
except ImportError:
    if os.environ.get("DBP_REQUIRE_HOSTED"):  # set by CI so that a broken install fails instead of skipping
        raise
    hosted = None


class Model:
    additional_properties = {}
    STORES_BY_DEFAULT = False

    def __init__(self, delay=0, finalizer_delay=0):
        self.delay, self.finalizer_delay = delay, finalizer_delay
        self.closed = False

    def get_response(self, messages, *, stream=False, **kwargs):
        from agent_framework import ChatResponse, ChatResponseUpdate, Content, ResponseStream

        async def updates():
            try:
                await asyncio.sleep(self.delay)
                yield ChatResponseUpdate(
                    contents=[Content.from_text('{"action":"defer","reason":"Missing evidence","evidence_ids":[]}')],
                    role="assistant", response_id="unit-response", finish_reason="stop")
            finally:
                self.closed = True

        async def finalize(items):
            await asyncio.sleep(self.finalizer_delay)
            return ChatResponse.from_updates(items)

        result = ResponseStream(updates(), finalizer=finalize)
        return result if stream else result.get_final_response()


@unittest.skipUnless(hosted, "Install agents/hosted/requirements.txt to test the hosted agent source.")
class HostedTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, model):
        with patch.object(hosted, "FoundryChatClient", return_value=model), patch.dict(os.environ, ENV):
            return hosted.make_agent()

    async def test_supplier_stream_preserves_updates_and_final_response(self):
        stream = self.agent(Model()).run("Assess task unit-task-001.", stream=True)
        parts = [update.text async for update in stream]
        result = await stream.get_final_response()
        self.assertEqual("".join(parts), result.text)
        self.assertEqual(json.loads(result.text)["action"], "defer")
        self.assertEqual(result.response_id, "unit-response")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual((await self.agent(Model()).run("Assess task unit-task-001.")).text, result.text)

    async def test_responses_host_completes_normal_and_streaming_requests(self):
        import httpx
        from agent_framework_foundry_hosting import ResponsesHostServer

        with patch.dict(os.environ, {"OTEL_SDK_DISABLED": "true"}):
            server = ResponsesHostServer(self.agent(Model()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://unit") as client:
            for stream in (False, True):
                with self.subTest(stream=stream):
                    response = await client.post("/responses", json={
                        "input": "Assess task unit-task-001.", "store": False, "stream": stream})
                    self.assertEqual(response.status_code, 200, response.text)
                    if stream:
                        events = [json.loads(line[6:]) for line in response.text.splitlines()
                                  if line.startswith("data: {")]
                        self.assertEqual(events[-1]["type"], "response.completed")
                        result = events[-1]["response"]
                    else:
                        result = response.json()
                    self.assertEqual(result["status"], "completed", result.get("error"))
                    self.assertEqual(json.loads(result["output"][0]["content"][0]["text"])["action"], "defer")

    async def test_nonstream_budget_cancels_slow_work(self):
        model = Model(delay=.1)
        with patch.object(hosted, "ACTIVATION_SECONDS", .01), self.assertRaises(TimeoutError):
            await self.agent(model).run("Assess task unit-task-001.")
        self.assertTrue(model.closed)

    async def test_stream_budget_covers_lazy_iteration_and_finalization(self):
        for model in (Model(delay=.1), Model(finalizer_delay=.1)):
            with self.subTest(model=vars(model)), patch.object(hosted, "ACTIVATION_SECONDS", .01):
                stream = self.agent(model).run("Assess task unit-task-001.", stream=True)
                with self.assertRaises(TimeoutError):
                    await stream.get_final_response()
                self.assertTrue(model.closed)

    async def test_closing_stream_closes_the_model_iterator(self):
        model = Model()
        stream = self.agent(model).run("Assess task unit-task-001.", stream=True)
        await anext(stream)
        await stream.close()
        self.assertTrue(model.closed)

    async def test_read_section_refuses_other_sections_and_hosts(self):
        with patch.dict(os.environ, {"PASSPORT_APP_URL": "http://passport.example", "PASSPORT_APP_AUDIENCE": "api://x"}):
            for task, section in (("task-0001", "state"), ("../bad", "evidence"), ("task-0001", "evidence")):
                with self.subTest(task=task, section=section), self.assertRaises(ValueError):
                    await hosted.read_section.func(task, section, "a" * 32)
            with self.assertRaises(ValueError):
                await hosted.read_section.func("task-0001", "evidence", "invalid")

    def test_tool_schema_names_only_the_role_sections(self):
        schema = hosted.read_section.parameters()["properties"]["section"]
        self.assertEqual(schema["enum"], ["evidence", "reviews"])

    async def test_invocations_handler_and_decision_shape(self):
        with patch.object(hosted, "ROLE", "lifecycle"), patch.object(hosted, "make_agent") as agent, \
                patch("azure.ai.agentserver.invocations.InvocationAgentServerHost") as server:
            handlers = []
            server.return_value.invoke_handler.side_effect = lambda handler: handlers.append(handler) or handler
            hosted.main()
            decision = {"action": "record_soh", "reason": "Record the permitted event.", "evidence_ids": []}
            agent.return_value.run = AsyncMock(return_value=SimpleNamespace(
                text=json.dumps(decision), usage_details={"input_token_count": 120, "output_token_count": 30}))
            payload = {"task_id": "health-task-001", "attempt_id": "a" * 32}
            result = await handlers[0](SimpleNamespace(json=AsyncMock(return_value=payload)))
            self.assertEqual(json.loads(result.body), {
                "decision": {**decision, "policy_title": None, "policy_text": None},
                "usage": {"input_tokens": 120, "output_tokens": 30}})
            for invalid in (None, ["task_id"], {"task_id": 12345678}, {"task_id": "../bad"},
                            {"task_id": "health-task-001"}):
                result = await handlers[0](SimpleNamespace(json=AsyncMock(return_value=invalid)))
                self.assertEqual(result.status_code, 422)
            for action in ("publish", "no_issue", "approve_second_life"):
                with self.subTest(action=action):
                    agent.return_value.run = AsyncMock(return_value=SimpleNamespace(text=json.dumps(
                        {**decision, "action": action})))
                    result = await handlers[0](SimpleNamespace(json=AsyncMock(
                        return_value=payload)))
                    self.assertEqual(result.status_code, 422)
                    self.assertNotIn("decision", json.loads(result.body))

    async def test_agent_uses_contract_two_role_instructions(self):
        with patch.object(hosted, "ROLE", "lifecycle"), patch.object(hosted, "FoundryChatClient"), \
                patch.object(hosted, "BudgetedAgent") as agent, patch.dict(os.environ, ENV):
            hosted.make_agent()
        text = agent.call_args.kwargs["instructions"]
        self.assertIn("Contract dbp-agent-2", text)
        self.assertIn("You cannot approve or reject a second life", text)
        self.assertNotIn("Supplier:", text)

    async def test_responses_host_and_supported_model_options(self):
        from agent_framework.foundry import FoundryChatOptions
        with patch.object(hosted, "FoundryChatClient"), patch.object(hosted, "BudgetedAgent") as agent, \
                patch("agent_framework_foundry_hosting.ResponsesHostServer") as server, patch.dict(os.environ, ENV):
            hosted.make_agent()
            self.assertLessEqual(set(agent.call_args.kwargs["default_options"]),
                                 FoundryChatOptions.__annotations__.keys())
            with patch.object(hosted, "make_agent", return_value=agent.return_value):
                hosted.main()
            server.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
