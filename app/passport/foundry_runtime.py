"""Real Foundry invocation only. Offline test doubles are injected, never a cloud fallback."""

import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from openai import OpenAI

from .agent_contracts import CONTRACT, ROLES
from .prompt_shields import PromptShield, content_safety_endpoint

PROTOCOLS = {"openai/responses": "responses", "openai/v1/responses": "responses", "invocations": "invocations"}


def _correlation(response, headers=None):
    headers = headers or {}
    identifiers = {"response_id": response.id, "session_id": headers.get("x-agent-session-id"),
                   "request_id": headers.get("x-request-id") or headers.get("apim-request-id")}
    return "; ".join(f"{key}={value}" for key, value in identifiers.items() if value)


def response_failure(kind, response, headers=None):
    """Retain the provider cause and correlation IDs, never the full response or request headers."""
    if response.error:
        detail = f"{response.error.code}: {response.error.message}"
    elif response.incomplete_details:
        detail = f"Incomplete: {response.incomplete_details.reason}"
    else:
        detail = "Foundry returned no error details."
    return RuntimeError(f"{kind} response did not complete: {response.status}. {detail} "
                        f"[{_correlation(response, headers)}]")


def response_decision(kind, response, headers=None):
    """Parse the final JSON. A completed response without text is a model route failure, not malformed JSON."""
    if not (response.output_text or "").strip():
        items = ", ".join(sorted({item.type for item in response.output})) or "none"
        tokens = response.usage.output_tokens if response.usage else "not reported"
        raise RuntimeError(f"{kind} response completed without answer text (output items: {items}; output "
                           f"tokens: {tokens}), so there is no decision. [{_correlation(response, headers)}]")
    return json.loads(response.output_text)


def hosted_endpoint(project_endpoint, name, endpoint):
    """Validate a hosted agent's protocol URL; return (protocol, request URL or OpenAI base URL, query)."""
    project, parsed = urlsplit(project_endpoint.rstrip("/")), urlsplit(endpoint or "")
    prefix = f"{project.path}/agents/{name}/endpoint/protocols/"
    if (parsed.scheme != "https" or parsed.netloc.lower() != project.netloc.lower() or parsed.fragment
            or not parsed.path.lower().startswith(prefix.lower())):
        raise ValueError(f"The {name} endpoint must belong to the configured Foundry project and agent.")
    protocol = PROTOCOLS.get(parsed.path[len(prefix):].rstrip("/").lower())
    if not protocol:
        raise ValueError(f"The {name} endpoint must use the hosted Responses or Invocations protocol path.")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("api-version", "v1")
    path = parsed.path.rstrip("/")
    if protocol == "responses":
        return protocol, urlunsplit(("https", parsed.netloc, path[:-len("/responses")], "", "")), query
    return protocol, urlunsplit(("https", parsed.netloc, path, urlencode(query), "")), query


class FoundryRunner:
    def __init__(self, config, credential=None):
        self.config = config
        self.credential = credential or DefaultAzureCredential()
        endpoint = config.get("project_endpoint", "")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".services.ai.azure.com"):
            raise ValueError("Configure an HTTPS Foundry project endpoint under services.ai.azure.com.")
        self.endpoint = endpoint.rstrip("/")
        self.shield = PromptShield(content_safety_endpoint(endpoint), self.credential)

    def run(self, role, task_id, context, reader, *, attempt_id=None, prompt_shadow=False):
        spec = self.config["roles"][role]
        if spec["kind"] != ROLES[role]["kind"] and not (prompt_shadow and spec["kind"] == "prompt"):
            raise ValueError("Configured agent hosting kind differs from the agreed agent role.")
        started = time.monotonic()
        metadata = {"hosting": spec["kind"], "agent_name": spec["name"], "agent_version": spec["version"],
                    "model": spec["model"], "model_version": spec["model_version"], "contract": CONTRACT,
                    "evidence_level": "foundry_response", "input_tokens": None, "output_tokens": None}
        if spec["kind"] == "hosted":
            # Endpoints are discovered after deployment, never accepted from a model or HTTP request.
            protocol, endpoint, query = hosted_endpoint(self.endpoint, spec["name"], spec["endpoint"])
            if protocol != ("invocations" if role == "lifecycle" else "responses"):
                raise ValueError(f"The {role} endpoint uses the wrong hosted protocol.")
            token = self.credential.get_token("https://ai.azure.com/.default").token
            if role == "lifecycle":
                with httpx.Client(timeout=120) as client:
                    response = client.post(endpoint, json={"task_id": task_id, "attempt_id": attempt_id},
                                           headers={"Authorization": f"Bearer {token}"})
                    response.raise_for_status()
                    payload = response.json()
                decision = payload["decision"]
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                metadata.update({key: usage[key] for key in ("input_tokens", "output_tokens")
                                 if type(usage.get(key)) is int and usage[key] >= 0})
            else:
                with OpenAI(base_url=endpoint, api_key=token, default_query=query, timeout=120,
                            max_retries=0) as client:
                    raw = client.responses.with_raw_response.create(
                        input=f"Assess task {task_id}, attempt {attempt_id}. Use its pinned sections.",
                        max_output_tokens=2500)
                    response = raw.parse()
                if response.status != "completed":
                    raise response_failure("Hosted", response, raw.headers)
                decision = response_decision("Hosted", response, raw.headers)
                if response.usage:
                    metadata.update(input_tokens=response.usage.input_tokens,
                                    output_tokens=response.usage.output_tokens)
        else:
            input_tokens = output_tokens = calls = 0
            usage_known = True
            with AIProjectClient(endpoint=self.endpoint, credential=self.credential) as project:
                with project.get_openai_client() as client:
                    client = client.with_options(max_retries=0)
                    reference = {"agent_reference": {"name": spec["name"], "version": spec["version"],
                                                     "type": "agent_reference"}}
                    messages = [{"role": "user", "content":
                                 f"Assess task {task_id}, attempt {attempt_id}. Use its pinned sections."}]
                    for _ in range(9):
                        remaining = 120 - (time.monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError("Agent activation exceeded 120 seconds.")
                        response = client.responses.create(
                            input=messages, extra_body=reference, max_output_tokens=2500,
                            timeout=remaining, store=False, include=["reasoning.encrypted_content"])
                        if response.status not in ("completed",):
                            raise response_failure("Prompt", response)
                        if response.usage:
                            input_tokens += response.usage.input_tokens
                            output_tokens += response.usage.output_tokens
                        else:
                            usage_known = False
                        if input_tokens + output_tokens > 24000:
                            raise RuntimeError("Agent exceeded the 24,000 reported-token activation budget.")
                        functions = [item for item in response.output if item.type == "function_call"]
                        if not functions:
                            decision = response_decision("Prompt", response)
                            break
                        messages.extend(item.model_dump(exclude_none=True) for item in response.output)
                        for item in functions:
                            calls += 1
                            if calls > 8 or item.name != "read_section":
                                raise RuntimeError("Agent requested an unavailable tool or exceeded eight tool calls.")
                            args = json.loads(item.arguments)
                            if set(args) != {"section"}:
                                raise ValueError("Unexpected tool arguments.")
                            value = reader(task_id, role, args["section"])
                            messages.append({"type": "function_call_output", "call_id": item.call_id,
                                             "output": json.dumps(value)})
                    else:
                        raise RuntimeError("Agent failed to finish within its step budget.")
            metadata.update(input_tokens=input_tokens if usage_known else None,
                            output_tokens=output_tokens if usage_known else None)
        metadata["latency_ms"] = round((time.monotonic() - started) * 1000)
        if prompt_shadow:
            metadata["hosting"] = "prompt_evaluation_shadow"
        return decision, metadata
