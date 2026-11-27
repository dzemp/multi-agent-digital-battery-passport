"""Machine-readable marking of AI-generated text at application level (EU AI Act Art. 50(2)).

Every non-empty text field an agent returns (reason, policy_title, policy_text) gets one append-only record in
AiContentProvenance, written in the same transaction as the text. The record states which agent and model wrote it,
when, where the text is stored and a SHA-256 fingerprint of the normalised text. It names no people: the person or
workflow that started the task stays in AgentTasks. The identifier is derived from the task and field, so screens
and APIs can link a label without another lookup. Model-level text watermarking is a separate layer, documented in
docs/eu-ai-act.md.
"""

import unicodedata

from . import domain
from .agent_contracts import CONTRACT, fingerprint

AI_HOSTING = frozenset({"hosted", "prompt", "prompt_evaluation_shadow"})
SCRIPTED_HOSTING = frozenset({"local_scripted_double", "local_test_double"})
AI_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
SCRIPTED_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/algorithmicMedia"
TEXT_FIELDS = ("reason", "policy_title", "policy_text")
PUBLIC_KEYS = ("provenance_id", "task_id", "role", "field", "stored_in", "ai_generated", "digital_source_type",
               "generator", "agent_name", "agent_version", "model", "model_version", "hosting", "generated_utc")
LIMITS = {"agent_name": 100, "agent_version": 50, "model": 100, "model_version": 50, "hosting": 50}


def normalise(text):
    """NFC and collapsed whitespace, so a copied label or a re-wrapped paragraph still matches."""
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def text_sha256(text):
    return domain.sha256(normalise(text).encode("utf-8"))


def provenance_id(task_id, field):
    return "PV-" + fingerprint({"task_id": task_id, "field": field})[:61]


def ai_generated(metadata):
    """Only the declared rule-based doubles are not AI. Any other or unknown runner is marked as AI-generated."""
    return (metadata or {}).get("hosting") not in SCRIPTED_HOSTING


def execution(metadata):
    """The agent and model facts the record keeps, trimmed to the column sizes."""
    metadata = metadata or {}
    return {key: str(metadata[key])[:limit] if metadata.get(key) not in (None, "") else None
            for key, limit in LIMITS.items()}


def records(task, decision, metadata, stored_in, now):
    """One provenance row per non-empty text field of an accepted decision. stored_in maps field -> locations."""
    ai = ai_generated(metadata)
    origin = execution(metadata)
    rows = []
    for field in TEXT_FIELDS:
        text = decision.get(field)
        if not text:
            continue
        rows.append({"provenance_id": provenance_id(task["task_id"], field), "task_id": task["task_id"],
                     "role": task["role"], "field": field, "stored_in": ",".join(stored_in[field]),
                     "ai_generated": int(ai), "digital_source_type": AI_SOURCE if ai else SCRIPTED_SOURCE,
                     "generator": CONTRACT, **origin, "text_sha256": text_sha256(text), "text_chars": len(text),
                     "generated_utc": now, "human_modified": 0})
    return rows


def public(row):
    """What screens, APIs and the verify endpoint may show: no fingerprint, no text and no people."""
    return {**{key: row[key] for key in PUBLIC_KEYS}, "ai_generated": bool(row["ai_generated"])} if row else None


def mark(row):
    """The compact mark that travels next to an AI text in a JSON projection."""
    if not row:
        return None
    return {"provenance_id": row["provenance_id"], "ai_generated": bool(row["ai_generated"]),
            **{key: row[key] for key in ("digital_source_type", "model", "model_version", "generated_utc")}}
