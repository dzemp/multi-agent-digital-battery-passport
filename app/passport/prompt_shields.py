"""Azure AI Content Safety Prompt Shields: screen supplier evidence for hidden instructions before a model reads it.

Supplier documents are third-party content, so they go in the API's ``documents`` field (document attacks). The
service accepts at most five documents and 10,000 characters per call; longer evidence is split into overlapping
pieces so that an instruction cannot hide across a split. The application decides what happens on a detection.
"""

import json
from urllib.parse import urlsplit

API_VERSION = "2024-09-01"
SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_DOCUMENTS = 5
MAX_CHARS = 10_000
OVERLAP = 500
USER_PROMPT = "Assess the pinned supplier evidence for this lot."
ACTOR = "system:prompt-shield"
REVIEW_REASON = ("Prompt Shields detected instructions aimed at an AI model in the cited supplier evidence. "
                 "Check the document and resolve with new evidence; the model did not read it.")


def content_safety_endpoint(project_endpoint):
    """https://<account>.services.ai.azure.com/api/projects/<p> -> https://<account>.cognitiveservices.azure.com"""
    parsed = urlsplit(project_endpoint or "")
    host = (parsed.hostname or "").lower()
    suffix = ".services.ai.azure.com"
    account = host[:-len(suffix)] if host.endswith(suffix) else ""
    if parsed.scheme != "https" or not account or "." in account:
        raise ValueError("Configure an HTTPS Foundry project endpoint under services.ai.azure.com.")
    return f"https://{account}.cognitiveservices.azure.com"


def pieces(evidence):
    """(evidence ID, text) per evidence item, each text at most MAX_CHARS - len(USER_PROMPT) characters."""
    size = MAX_CHARS - len(USER_PROMPT)
    result = []
    for item in evidence:
        text = json.dumps(item["content"], sort_keys=True, ensure_ascii=False)
        start = 0
        while True:
            result.append((item["id"], text[start:start + size]))
            if start + size >= len(text):
                break
            start += size - OVERLAP
    return result


def batches(items):
    """Group pieces into calls of at most five documents and MAX_CHARS characters including the user prompt."""
    calls, current, used = [], [], len(USER_PROMPT)
    for item in items:
        if current and (len(current) == MAX_DOCUMENTS or used + len(item[1]) > MAX_CHARS):
            calls.append(current)
            current, used = [], len(USER_PROMPT)
        current.append(item)
        used += len(item[1])
    if current:
        calls.append(current)
    return calls


class Screener:
    """Contract for the supplier evidence gate: screen(evidence) returns flagged, documents, calls and api_version.
    A runner opts in by exposing an instance as runner.shield."""

    def screen(self, evidence):
        raise NotImplementedError


class PromptShield(Screener):
    def __init__(self, endpoint, credential, client_factory=None):
        parsed = urlsplit(endpoint or "")
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".cognitiveservices.azure.com"):
            raise ValueError("Prompt Shields needs an HTTPS cognitiveservices.azure.com endpoint.")
        self.url = f"{endpoint.rstrip('/')}/contentsafety/text:shieldPrompt?api-version={API_VERSION}"
        self.credential = credential
        self.client_factory = client_factory

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()
        import httpx
        return httpx.Client(timeout=30)

    def screen(self, evidence):
        """Return {"flagged": [evidence IDs], "documents": n, "calls": n, "api_version": ...}. Raise on any doubt:
        the caller treats an unscreened lot as not ready for a model."""
        items = pieces(evidence)
        flagged = set()
        token = self.credential.get_token(SCOPE).token
        calls = batches(items)
        with self._client() as client:
            for call in calls:
                response = client.post(self.url, headers={"Authorization": f"Bearer {token}"},
                                       json={"userPrompt": USER_PROMPT, "documents": [text for _, text in call]})
                response.raise_for_status()
                analysis = response.json().get("documentsAnalysis")
                if not isinstance(analysis, list) or len(analysis) != len(call) or \
                        any(not isinstance(a, dict) or type(a.get("attackDetected")) is not bool for a in analysis):
                    raise ValueError("Prompt Shields returned an unexpected document analysis.")
                flagged |= {evidence_id for (evidence_id, _), a in zip(call, analysis, strict=True)
                            if a["attackDetected"]}
        return {"flagged": sorted(flagged), "documents": len(items), "calls": len(calls), "api_version": API_VERSION}
