"""Compare three model assignments on identical cases. No writes to operational passports.

The local mode checks the scoring code against the labelled expected answers and the scripted double's rules
against the same cases; neither is a model. The model mode creates temporary prompt agents, makes billable model
calls and deletes those agents afterwards.
"""

import json
import math
import statistics
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parents[1] / "app"
for path in (APP, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cases import cases, shield_cases  # noqa: E402
from passport.agent_contracts import (  # noqa: E402
    ACTIONS, ROLES, SECTIONS, Decision, instructions, read_section, tool_definition)

ASSIGNMENTS = ("role_fit", "compact_all", "frontier_all")
COMPACT_MODEL = "gpt-5.6-luna"
FRONTIER_MODEL = ROLES["supplier"]["model"]
TOKEN_LIMIT = 100_000
NOT_MEASURED = ["hosted topology performance", "total Azure cost", "legal correctness",
                "production accuracy", "human review effort"]


def score(case, decision, sections):
    try:
        parsed = Decision.model_validate(decision)
    except ValueError:
        return {"pass": False, "action_ok": False, "citations_ok": False, "grounded": False}
    action_ok = parsed.action == case["expected"] and parsed.action in ACTIONS[case["role"]]
    citations_ok = set(parsed.evidence_ids) == set(case["citations"])
    grounded = set(SECTIONS[case["role"]]) <= set(sections)
    return {"pass": action_ok and citations_ok and grounded, "action_ok": action_ok,
            "citations_ok": citations_ok, "grounded": grounded}


def _model_cost(prices, execution):
    if not prices:
        return None
    model = execution.get("model")
    rates = (prices.get("models") or {}).get(model or "")
    tokens_in, tokens_out = execution.get("input_tokens"), execution.get("output_tokens")
    if not rates or tokens_in is None or tokens_out is None:
        return None
    return (tokens_in * rates["input_per_million"] + tokens_out * rates["output_per_million"]) / 1_000_000


def summarize(rows, prices=None):
    groups = {}
    for assignment in sorted({r["assignment"] for r in rows}):
        selected = [r for r in rows if r["assignment"] == assignment]
        latency = sorted(r["execution"]["latency_ms"] for r in selected
                         if r.get("execution", {}).get("latency_ms") is not None)
        costs = [_model_cost(prices, r.get("execution") or {}) for r in selected]
        priced = bool(selected) and all(cost is not None for cost in costs)
        groups[assignment] = {
            "cases": len(selected), "passed": sum(r["score"]["pass"] for r in selected),
            "critical_failures": sum(r["critical"] and not r["score"]["pass"] for r in selected),
            "p50_ms": statistics.median(latency) if latency else None,
            "p95_ms": latency[math.ceil(len(latency) * .95) - 1] if latency else None,
            "input_tokens": sum(r["execution"]["input_tokens"] for r in selected) if
                all(r.get("execution", {}).get("input_tokens") is not None for r in selected) else None,
            "output_tokens": sum(r["execution"]["output_tokens"] for r in selected) if
                all(r.get("execution", {}).get("output_tokens") is not None for r in selected) else None,
            "model_cost_total": round(sum(costs), 8) if priced else None,
            "model_cost_per_100_runs": round(sum(costs) / len(selected) * 100, 8) if priced else None,
            "model_cost_currency": prices.get("currency") if priced else None,
            "model_cost_usd": round(sum(costs), 8) if priced and prices.get("currency") == "USD" else None,
            "price_source": prices.get("source") if priced else None,
            "hosting_cost": None,
            "decision": "needs_human_quality_and_cost_review" if all(r["score"]["pass"] for r in selected)
                        else "do_not_promote"}
    return groups


def failed(report):
    return bool(report["fatal_error"] or report["cleanup_errors"]
                or not all(r["score"]["pass"] for r in report["rows"]))


def _write(output, name, value):
    (output / name).write_text(json.dumps(value, indent=2), encoding="utf-8")


def _report(output, evidence_level, rows, fatal_error=None, cleanup_errors=(), prices=None):
    report = {"evidence_level": evidence_level, "fatal_error": fatal_error,
              "cleanup_errors": list(cleanup_errors), "not_measured": NOT_MEASURED,
              "summary": summarize(rows, prices), "rows": rows}
    if evidence_level == "local_scoring_self_test":
        for summary in report["summary"].values():
            summary["decision"] = "scoring_self_test_only"
    _write(output, "report.json", report)
    return report


def _scripted_double():
    """The deterministic scripted double that drives the local story (scenario/scripted.py); not AI."""
    root = str(HERE.parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from scenario.scripted import ScriptedDouble
    return ScriptedDouble()


def run_local(output):
    """Score the labelled expected answers, then the scripted double's rule-based answers, on every case.
    This tests the scorer and the double's rules; it is NOT a model evaluation."""
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases():
        decision = {"action": case["expected"], "reason": "Expected fixture for scoring self-test only.",
                    "evidence_ids": case["citations"]}
        rows.append({"id": case["id"], "assignment": "scoring_self_test", "critical": case["critical"],
                     "score": score(case, decision, SECTIONS[case["role"]]), "execution": {},
                     "evidence_level": "local_not_a_model_benchmark"})
    double = _scripted_double()
    for case in cases():
        sections = []

        def reader(_task_id, role, section, case=case, sections=sections):
            sections.append(section)
            return read_section(role, case["context"], {"section": section})

        decision, _ = double.run(case["role"], f"eval-{case['id']}", case["context"], reader)
        rows.append({"id": case["id"], "assignment": "scripted_double", "critical": case["critical"],
                     "decision": decision, "score": score(case, decision, sections), "execution": {},
                     "evidence_level": "local_scripted_double_not_ai"})
    return _report(output, "local_scoring_self_test", rows)


def assignment_model(assignment, role):
    if assignment == "role_fit":
        return ROLES[role]["model"]
    return COMPACT_MODEL if assignment == "compact_all" else FRONTIER_MODEL


def _project(endpoint, credential):
    from azure.ai.projects import AIProjectClient
    return AIProjectClient(endpoint=endpoint, credential=credential)


def _runner(config, credential):
    from passport.foundry_runtime import FoundryRunner
    return FoundryRunner(config, credential=credential)


def _prompt_definition(model, role):
    from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
    return PromptAgentDefinition(model=model, instructions=instructions(role),
                                 tools=[FunctionTool(**tool_definition(role))])


def run_models(endpoint, credential, output, repeats=1, *, project_factory=None, runner_factory=None,
               definition_factory=None, prices=None, assignments=ASSIGNMENTS):
    """Create temporary prompt agents per assignment, run every case and always delete the agents."""
    if repeats not in range(1, 6):
        raise ValueError("repeats must be between 1 and 5.")
    assignments = ASSIGNMENTS if assignments is None else assignments
    if not assignments or len(set(assignments)) != len(assignments) or \
            any(assignment not in ASSIGNMENTS for assignment in assignments):
        raise ValueError(f"assignments must be a non-empty subset of {ASSIGNMENTS}.")
    project_factory = project_factory or _project
    runner_factory = runner_factory or _runner
    definition_factory = definition_factory or _prompt_definition
    output.mkdir(parents=True, exist_ok=True)
    rows, created, cleanup_errors = [], [], []
    fatal_error = None
    dataset = cases()
    prefix = "dbp-eval-" + uuid.uuid4().hex[:8]
    token_total = 0
    with project_factory(endpoint, credential) as project:
        try:
            for assignment in assignments:
                configs = {}
                for role in ROLES:
                    model = assignment_model(assignment, role)
                    version = next(s["version"] for s in ROLES.values() if s["model"] == model)
                    name = f"{prefix}-{assignment.replace('_', '-')}-{role}"
                    agent = project.agents.create_version(agent_name=name,
                                                          definition=definition_factory(model, role))
                    created.append((name, agent.version))
                    _write(output, "created-agents.json", created)
                    configs[role] = {"kind": "prompt", "name": name, "version": agent.version,
                                     "model": model, "model_version": version}
                runner = runner_factory({"project_endpoint": endpoint, "roles": configs}, credential)
                for repeat in range(repeats):
                    for case in dataset:
                        sections = []

                        def reader(_task_id, role, section, case=case, sections=sections):
                            value = read_section(role, case["context"], {"section": section})
                            sections.append(section)
                            return value

                        try:
                            decision, execution = runner.run(case["role"], f"eval-{case['id']}", case["context"],
                                                             reader, prompt_shadow=True)
                            scored = score(case, decision, sections)
                            token_total += (execution["input_tokens"] or 0) + (execution["output_tokens"] or 0)
                            row = {"decision": decision, "execution": execution, "score": scored}
                        except Exception as error:
                            row = {"error": f"{type(error).__name__}: {str(error)[:500]}",
                                   "execution": {}, "score": {"pass": False}}
                        rows.append({**row, "id": case["id"], "critical": case["critical"],
                                     "role": case["role"], "assignment": assignment, "repeat": repeat,
                                     "sections": sections, "evidence_level": "prompt_model_comparison"})
                        _write(output, "observations.json", rows)
                        if token_total > TOKEN_LIMIT:
                            raise RuntimeError("Stopped at 100,000 reported tokens; partial observations are saved.")
        except Exception as error:
            fatal_error = f"{type(error).__name__}: {error}"
        finally:
            for name, version in created:
                try:
                    project.agents.delete_version(agent_name=name, agent_version=version)
                except Exception as error:
                    cleanup_errors.append({"name": name, "version": version, "error": str(error)[:500]})
    return _report(output, "prompt_shadow_model_comparison", rows, fatal_error, cleanup_errors, prices)


def _shield(endpoint, credential):
    from passport.prompt_shields import PromptShield, content_safety_endpoint
    return PromptShield(content_safety_endpoint(endpoint), credential)


def run_shield(endpoint, credential, output, *, shield_factory=None):
    """Screen every Prompt Shields probe through the deployed Content Safety endpoint (billable per call).
    A probe passes when exactly its attack evidence is flagged; attacks missed and benign notes flagged are counted
    separately. No model reads the evidence and no passport is written."""
    output.mkdir(parents=True, exist_ok=True)
    shield = (shield_factory or _shield)(endpoint, credential)
    rows, fatal_error = [], None
    for probe in shield_cases():
        try:
            result = shield.screen(probe["evidence"])
            row = {"flagged": result["flagged"], "calls": result["calls"], "documents": result["documents"],
                   "pass": set(result["flagged"]) == set(probe["attack_ids"])}
        except Exception as error:
            row = {"error": f"{type(error).__name__}: {str(error)[:500]}", "pass": False}
        rows.append({"id": probe["id"], "attack_ids": probe["attack_ids"], **row})
        _write(output, "observations.json", rows)
    attacks = [r for r in rows if r["attack_ids"]]
    benign = [r for r in rows if not r["attack_ids"]]
    summary = {
        "probes": len(rows), "passed": sum(r["pass"] for r in rows),
        "attacks_detected": sum("error" not in r and set(r["attack_ids"]) <= set(r["flagged"]) for r in attacks),
        "attacks": len(attacks),
        "false_positives": sum("error" not in r and bool(r["flagged"]) for r in benign), "clean": len(benign),
        "errors": sum("error" in r for r in rows),
        "decision": "keep_screening_with_human_review" if all(r["pass"] for r in rows) else "review_misses"}
    report = {"evidence_level": "prompt_shields_probe", "fatal_error": fatal_error, "cleanup_errors": [],
              "not_measured": ["production attack rate", "attacks in images or attachments",
                               "languages beyond English, German and French", "adaptive attackers"],
              "summary": {"prompt_shields": summary}, "rows": rows}
    _write(output, "report.json", report)
    return report
