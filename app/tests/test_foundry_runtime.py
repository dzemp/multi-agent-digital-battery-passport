"""Foundry response parsing and failure diagnostics, without network calls."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import helpers  # noqa: F401 - adds the app to sys.path
from passport import foundry_runtime

ENDPOINT = "https://unit.services.ai.azure.com/api/projects/passport"
CONFIG = {"project_endpoint": ENDPOINT, "roles": {"supplier": {
    "kind": "hosted", "name": "dbp-supplier", "version": "1", "model": "unit", "model_version": "1",
    "endpoint": f"{ENDPOINT}/agents/dbp-supplier/endpoint/protocols/openai/responses?api-version=v1"}}}


class HostedResponses(unittest.TestCase):
    def run_response(self, response):
        raw = Mock(headers={"x-agent-session-id": "unit-session", "x-request-id": "unit-request",
                            "authorization": "Bearer do-not-report"})
        raw.parse.return_value = response
        with patch.object(foundry_runtime, "OpenAI") as client:
            create = client.return_value.__enter__.return_value.responses.with_raw_response.create
            create.return_value = raw
            runner = foundry_runtime.FoundryRunner(CONFIG, credential=Mock())
            return runner.run("supplier", "unit-task-001", {}, Mock())

    def test_completed_response_retains_decision_and_usage(self):
        decision = {"action": "defer", "reason": "Missing evidence", "evidence_ids": []}
        result, metadata = self.run_response(SimpleNamespace(
            status="completed", output_text=json.dumps(decision),
            usage=SimpleNamespace(input_tokens=42, output_tokens=12)))
        self.assertEqual(result, decision)
        self.assertEqual((metadata["input_tokens"], metadata["output_tokens"]), (42, 12))

    def test_failed_response_preserves_provider_error_and_correlation_only(self):
        response = SimpleNamespace(
            status="failed", id="unit-response", incomplete_details=None,
            error=SimpleNamespace(code="server_error",
                                  message="Expected ResponseStream for streaming, got AgentResponse"))
        with self.assertRaisesRegex(RuntimeError, "server_error: Expected ResponseStream") as raised:
            self.run_response(response)
        message = str(raised.exception)
        for identifier in ("response_id=unit-response", "session_id=unit-session", "request_id=unit-request"):
            self.assertIn(identifier, message)
        self.assertNotIn("do-not-report", message)

    def test_lifecycle_usage_is_recorded_without_overwriting_other_metadata(self):
            config = {"project_endpoint": ENDPOINT, "roles": {"lifecycle": {
                "kind": "hosted", "name": "dbp-lifecycle", "version": "1", "model": "unit", "model_version": "1",
                "endpoint": f"{ENDPOINT}/agents/dbp-lifecycle/endpoint/protocols/invocations"}}}
            decision = {"action": "defer", "reason": "Event incomplete", "evidence_ids": []}
            for usage, expected in (
                ({"input_tokens": 90, "output_tokens": 20, "hosting": "x", "model": "y"}, (90, 20)),
                ({"input_tokens": "90", "output_tokens": -1}, (None, None)), ([1, 2], (None, None))
            ):
                with self.subTest(usage=usage), patch.object(foundry_runtime.httpx, "Client") as client:
                    post = client.return_value.__enter__.return_value.post
                    post.return_value.json.return_value = {"decision": decision, "usage": usage}
                    result, metadata = foundry_runtime.FoundryRunner(config, credential=Mock()).run(
                        "lifecycle", "unit-task-003", {}, Mock())
                self.assertEqual(result, decision)
                self.assertEqual((metadata["input_tokens"], metadata["output_tokens"]), expected)
                self.assertEqual((metadata["hosting"], metadata["model"]), ("hosted", "unit"))

    def test_missing_error_and_incomplete_reason_are_explicit(self):
        for details, expected in ((None, "Foundry returned no error details"),
                                  (SimpleNamespace(reason="max_output_tokens"), "Incomplete: max_output_tokens")):
            with self.subTest(details=details), self.assertRaisesRegex(RuntimeError, expected):
                self.run_response(SimpleNamespace(status="incomplete", id="unit-response",
                                                  error=None, incomplete_details=details))

    def test_completed_response_without_text_is_reported_not_parsed_as_json(self):
        for output, usage, expected in (
            ([], SimpleNamespace(input_tokens=0, output_tokens=0), "output items: none; output tokens: 0"),
            ([SimpleNamespace(type="reasoning")], None, "output items: reasoning; output tokens: not reported"),
        ):
            response = SimpleNamespace(status="completed", id="unit-response", output=output, output_text="",
                                       usage=usage)
            with self.subTest(expected=expected), self.assertRaises(RuntimeError) as raised:
                self.run_response(response)
            message = str(raised.exception)
            self.assertIn(f"completed without answer text ({expected})", message)
            self.assertIn("response_id=unit-response; session_id=unit-session; request_id=unit-request", message)


class PromptResponses(unittest.TestCase):
    def test_agent_reference_is_sent_on_initial_and_tool_continuation_requests(self):
        config = {"project_endpoint": ENDPOINT, "roles": {"carbon": {
            "kind": "prompt", "name": "dbp-carbon", "version": "2", "model": "unit", "model_version": "1"}}}
        decision = {"action": "defer", "reason": "Missing inputs", "evidence_ids": []}
        function = {"type": "function_call", "name": "read_section", "call_id": "read-state",
                    "arguments": json.dumps({"section": "state"})}
        tool = SimpleNamespace(**function, model_dump=Mock(return_value=function))
        reader = Mock(return_value={"readiness": "inputs_incomplete"})
        with patch.object(foundry_runtime, "AIProjectClient") as factory:
            project = factory.return_value.__enter__.return_value
            client = project.get_openai_client.return_value.__enter__.return_value.with_options.return_value
            client.responses.create.side_effect = [
                SimpleNamespace(status="completed", output=[tool],
                                usage=SimpleNamespace(input_tokens=10, output_tokens=2)),
                SimpleNamespace(status="completed", output=[], output_text=json.dumps(decision),
                                usage=SimpleNamespace(input_tokens=8, output_tokens=4))]
            result, metadata = foundry_runtime.FoundryRunner(config, credential=Mock()).run(
                "carbon", "unit-task-002", {}, reader)
        self.assertEqual(client.responses.create.call_count, 2)
        for request in client.responses.create.call_args_list:
            self.assertEqual(request.kwargs["extra_body"], {"agent_reference": {
                "name": "dbp-carbon", "version": "2", "type": "agent_reference"}})
        reader.assert_called_once_with("unit-task-002", "carbon", "state")
        self.assertEqual(result, decision)
        self.assertEqual((metadata["input_tokens"], metadata["output_tokens"]), (18, 6))
