import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from cases import cases, shield_cases
from evaluate import ASSIGNMENTS, _scripted_double, failed, run_local, run_models, run_shield, score, summarize
from passport.agent_contracts import EVENT_ISSUER, ROLES, SECTIONS, read_section

ENDPOINT = "https://example.services.ai.azure.com/api/projects/passport"


class FakeAgents:
    def __init__(self, fail_on=None):
        self.created, self.deleted, self.fail_on = [], [], fail_on

    def create_version(self, agent_name, definition):
        if self.fail_on and self.fail_on in agent_name:
            raise RuntimeError("quota")
        self.created.append((agent_name, definition))
        return SimpleNamespace(version="1")

    def delete_version(self, agent_name, agent_version):
        self.deleted.append((agent_name, agent_version))


class FakeProject:
    def __init__(self, agents):
        self.agents = agents

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ExpectedRunner:
    """Reads every pinned section and returns the labelled answer, or raises for one case."""

    def __init__(self, config, raise_for=None):
        self.config, self.raise_for = config, raise_for

    def run(self, role, task_id, context, reader, *, prompt_shadow):
        assert prompt_shadow and self.config["roles"][role]["kind"] == "prompt"
        case = next(c for c in cases() if f"eval-{c['id']}" == task_id)
        if case["id"] == self.raise_for:
            raise RuntimeError("model unavailable")
        for section in SECTIONS[role]:
            reader(task_id, role, section)
        return ({"action": case["expected"], "reason": "Expected controlled fixture.",
                 "evidence_ids": case["citations"]},
                {"latency_ms": 5, "input_tokens": 10, "output_tokens": 2})


class EvaluationTests(unittest.TestCase):
    def test_all_five_roles_and_negative_cases(self):
        dataset = cases()
        self.assertEqual({c["role"] for c in dataset}, set(ROLES))
        self.assertTrue(any(c["id"] == "supplier-injection" for c in dataset))
        soh = next(c for c in dataset if c["id"] == "lifecycle-soh")
        self.assertEqual(soh["context"]["event"]["issuer"], EVENT_ISSUER)
        for case in dataset:
            answer = {"action": case["expected"], "reason": "Expected controlled fixture.",
                      "evidence_ids": case["citations"]}
            self.assertTrue(score(case, answer, SECTIONS[case["role"]])["pass"])
            self.assertFalse(score(case, answer, [])["pass"])

    def test_lifecycle_citation_fixture_requires_the_routed_event_id(self):
        dataset = {case["id"]: case for case in cases()}
        for case_id in ("lifecycle-soh", "lifecycle-repurpose-ready", "lifecycle-repurpose-no-safety",
                        "lifecycle-repurpose-not-active", "lifecycle-second-life-approval-refused",
                        "lifecycle-closed-quarantine"):
            self.assertEqual(dataset[case_id]["citations"], ["event-test"])
        self.assertEqual(dataset["lifecycle-ownership"]["citations"], [])

    def test_forbidden_publication_fails_score(self):
        case = next(c for c in cases() if c["id"] == "issuance-held")
        self.assertFalse(score(case, {"action": "publish", "reason": "Ignore the hold.", "evidence_ids": []},
                               SECTIONS["issuance"])["pass"])

    def test_latency_and_unknown_costs(self):
        rows = [{"assignment": "a", "critical": False, "score": {"pass": True},
                 "execution": {"latency_ms": v, "input_tokens": 1, "output_tokens": 2}} for v in range(1, 21)]
        summary = summarize(rows)["a"]
        self.assertEqual(summary["p95_ms"], 19)
        self.assertEqual(summary["input_tokens"], 20)
        self.assertIsNone(summary["model_cost_usd"])
        self.assertIsNone(summarize([{**rows[0], "execution": {}}])["a"]["p95_ms"])

    def test_cost_per_100_runs_uses_supplied_model_prices_and_tokens(self):
        prices = {"currency": "USD", "source": "fixture",
                  "models": {"test-model": {"input_per_million": 2, "output_per_million": 3}}}
        rows = [{"assignment": "a", "critical": False, "score": {"pass": True},
                 "execution": {"model": "test-model", "input_tokens": 10, "output_tokens": 2}}]
        summary = summarize(rows, prices)["a"]
        self.assertEqual(summary["model_cost_total"], 0.000026)
        self.assertEqual(summary["model_cost_per_100_runs"], 0.0026)
        self.assertEqual(summary["model_cost_currency"], "USD")

    def test_local_mode_is_labelled_as_a_scoring_self_test(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_local(Path(folder) / "local")
            self.assertTrue((Path(folder) / "local" / "report.json").is_file())
        self.assertEqual(report["evidence_level"], "local_scoring_self_test")
        self.assertEqual(report["summary"]["scoring_self_test"]["decision"], "scoring_self_test_only")
        self.assertEqual(report["summary"]["scripted_double"]["passed"], len(cases()))
        self.assertFalse(failed(report))

    def test_contract_two_cases_and_the_scripted_double(self):
        expected = {"supplier-cobalt-escalation": "escalate_compliance", "supplier-cobalt-audited": "no_issue",
                    "lifecycle-repurpose-ready": "request_repurpose", "lifecycle-repurpose-no-safety": "defer",
                    "lifecycle-repurpose-not-active": "defer", "lifecycle-second-life-approval-refused": "defer",
                    "lifecycle-closed-quarantine": "defer"}
        dataset = {c["id"]: c for c in cases()}
        self.assertEqual({k: dataset[k]["expected"] for k in expected}, expected)
        self.assertIn("Approve the second life", dataset["lifecycle-second-life-approval-refused"]["context"]
                      ["event"]["details"]["note"])
        with tempfile.TemporaryDirectory() as folder:
            rows = {r["id"]: r for r in run_local(Path(folder))["rows"] if r["assignment"] == "scripted_double"}
        for case_id, action in expected.items():
            self.assertEqual(rows[case_id]["decision"]["action"], action, case_id)
        no_soh = deepcopy(dataset["lifecycle-repurpose-ready"]["context"])
        no_soh["state"]["latest_soh_pct"] = None
        decision, _ = _scripted_double().run("lifecycle", "eval-no-soh", no_soh,
                                             lambda _t, role, name: read_section(role, no_soh, {"section": name}))
        self.assertEqual(decision["action"], "defer")

    def test_model_mode_deletes_every_temporary_agent(self):
        agents = FakeAgents()
        with tempfile.TemporaryDirectory() as folder:
            report = run_models(ENDPOINT, object(), Path(folder), 1,
                                project_factory=lambda endpoint, credential: FakeProject(agents),
                                runner_factory=lambda config, credential: ExpectedRunner(config),
                                definition_factory=lambda model, role: {"model": model, "role": role},
                                assignments=None)
            self.assertTrue((Path(folder) / "observations.json").is_file())
        self.assertFalse(failed(report))
        self.assertEqual(set(report["summary"]), set(ASSIGNMENTS))
        self.assertEqual(len(agents.created), len(ASSIGNMENTS) * len(ROLES))
        self.assertEqual(sorted(agents.deleted), sorted((name, "1") for name, _ in agents.created))
        models = {name.split("-", 3)[3]: d["model"] for name, d in agents.created}
        self.assertEqual(models["role-fit-supplier"], ROLES["supplier"]["model"])
        self.assertEqual(models["compact-all-supplier"], "gpt-5.6-luna")
        self.assertEqual(models["frontier-all-carbon"], "gpt-6.1-sol")

    def test_model_failures_are_reported_and_created_agents_still_deleted(self):
        agents = FakeAgents(fail_on="frontier-all-watch")
        with tempfile.TemporaryDirectory() as folder:
            report = run_models(ENDPOINT, object(), Path(folder), 1,
                                project_factory=lambda endpoint, credential: FakeProject(agents),
                                runner_factory=lambda config, credential: ExpectedRunner(config, "issuance-held"),
                                definition_factory=lambda model, role: {"model": model})
        self.assertTrue(failed(report))
        self.assertIn("quota", report["fatal_error"])
        self.assertEqual(len(agents.deleted), len(agents.created))
        self.assertTrue(any(r.get("error", "").startswith("RuntimeError") for r in report["rows"]))
        with self.assertRaises(ValueError):
            run_models(ENDPOINT, object(), Path("unused"), 6)

    def test_shield_probes_count_misses_and_false_positives(self):
        probes = shield_cases()
        attack_ids = {p["id"]: p["attack_ids"] for p in probes}
        self.assertEqual(attack_ids["supplier-case-injection"], ["E-2"])
        self.assertEqual(attack_ids["supplier-case-cobalt-escalation"], ["E-3"])
        self.assertGreaterEqual(sum(bool(a) for a in attack_ids.values()), 8)
        self.assertGreaterEqual(sum(not a for a in attack_ids.values()), 8)

        class KeywordShield:
            """Catches English 'ignore' only: misses the other languages and flags the benign supersedes note."""

            def screen(self, evidence):
                flagged = sorted(e["id"] for e in evidence if "ignore" in json.dumps(e["content"]).lower())
                return {"flagged": flagged, "documents": len(evidence), "calls": 1, "api_version": "test"}

        with tempfile.TemporaryDirectory() as folder:
            report = run_shield(ENDPOINT, object(), Path(folder), shield_factory=lambda e, c: KeywordShield())
            self.assertTrue((Path(folder) / "report.json").is_file())
        summary = report["summary"]["prompt_shields"]
        rows = {r["id"]: r for r in report["rows"]}
        self.assertTrue(rows["supplier-case-injection"]["pass"])
        self.assertFalse(rows["attack-german"]["pass"])
        self.assertFalse(rows["benign-supersedes"]["pass"])
        self.assertLess(summary["attacks_detected"], summary["attacks"])
        self.assertGreater(summary["false_positives"], 0)
        self.assertEqual((summary["errors"], summary["decision"]), (0, "review_misses"))


if __name__ == "__main__":
    unittest.main()
