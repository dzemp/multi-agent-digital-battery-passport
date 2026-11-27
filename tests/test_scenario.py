"""Exercise the scenario's acts (python -m dbp_mvp run) against an in-memory passport app.

The agents are a scripted double, not AI, and the "deployed" app is the same in-memory app reached through a fake
network: these tests show the acts, the gates, the human hand-off and the evidence registers, not that the deployed
app or a model behaves the same way.
"""

import contextlib
import io
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from fastapi.testclient import TestClient

from test_deploy import APP_ID, APP_URL, TOKEN
from test_fabric import Capacity, FakeFabric
from test_orchestrator import CAPACITY, FakeAzure, Workspace, make_config

from scenario import preview
from scenario.acts import FAIL, NoAnswer, PASS, Register, Runner, STOPPED, redact
from scenario.series import Series
from scenario.scripted import ACTS, LocalApp, ScriptedDouble, play

from data import simulator
from data.pipeline.flows import measure_tables
from dbp_mvp import MvpError, acts, cli, deploy, smoke, supply
from dbp_mvp.state import State
from passport import domain, exec_views, experience
from passport.agent_contracts import SECTIONS
from passport.agent_service import AgentService
from passport.auth import LOCAL_ROLES_HEADER, LOCAL_USER_HEADER

REVIEWER = {LOCAL_USER_HEADER: "series-reviewer", LOCAL_ROLES_HEADER: "Passport.Reviewer"}


def quiet(*_):
    pass


class Bench:
    """One in-memory app and series that several acts share, as the deployed app does."""

    def __init__(self, tag="T1", extra=0, double=None, app=None, root=None):
        self.tmp = None if root else tempfile.TemporaryDirectory(prefix="dbp-scenario-test-",
                                                                 ignore_cleanup_errors=True)
        self.root = Path(root or self.tmp.name)
        self.own = app is None
        self.app = app or LocalApp(self.root, double or ScriptedDouble())
        self.series = Series(tag, extra, output=self.root / "output")

    def run(self, *acts_):
        steps = [(act, dict(options)) for act, options in ACTS if act in acts_] \
            if all(isinstance(a, str) for a in acts_) else acts_
        return play(out=quiet, out_dir=self.root / "runs", acts=steps, app=self.app, series=self.series)

    def runner(self, act, **kwargs):
        return self.app.runner(self.series, Register(self.series, act, "local", out_dir=self.root / "runs"), quiet,
                               **kwargs)

    def view(self):
        return self.runner("status").observe()

    def close(self):
        if self.own:
            self.app.close()
        if self.tmp:
            self.tmp.cleanup()


class SupplierSmokeTests(unittest.TestCase):
    def test_b7_requires_hosted_section_callbacks_for_the_current_attempt(self):
        hosted = mock.Mock()
        bench = Bench(tag=None, double=hosted)
        self.addCleanup(bench.close)
        bench.app.seed(bench.runner("prepare"))
        sections = ["evidence", "reviews"]

        def propose(role, task_id, context, reader, *, attempt_id=None):
            evidence = []
            for name in sections:
                response = bench.app.agent.get(f"/agents/tasks/{task_id}/{name}", params={"attempt_id": attempt_id})
                if name == "evidence" and response.is_success:
                    evidence = [doc["id"] for doc in response.json()]
            return {"action": "no_issue", "reason": "Only the signed claim is present.",
                    "evidence_ids": evidence}, ScriptedDouble.METADATA

        def post(url, body, token):
            response = bench.app.staff.post(url.removeprefix(APP_URL), json=body)
            return response.status_code, response.text

        hosted.run.side_effect = propose
        az = mock.Mock()
        for _ in range(2):
            self.assertEqual(smoke.supplier_smoke(az, APP_URL, APP_ID, "smoke-complete-0", post), "found no issue")
        hosted.run.assert_called_once()
        sections.remove("reviews")
        with self.assertRaisesRegex(MvpError, "422: grounding_required.*reviews"):
            smoke.supplier_smoke(az, APP_URL, APP_ID, "smoke-partial-1", post)


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.bench = Bench()
        self.addCleanup(self.bench.close)

    def test_the_four_acts_publish_two_then_five_with_human_decisions_between(self):
        results = self.bench.run("prepare", "review", "complete", "aftercare")
        self.assertEqual({act: verdict for act, (verdict, _) in results.items()},
                         {"prepare": PASS, "review": PASS, "complete": PASS, "aftercare": PASS})
        series, prepare = self.bench.series, results["prepare"][1].data
        supplier = sorted(s["outcome"] for s in prepare["steps"] if s["role"] == "supplier")
        self.assertEqual(supplier, ["compliance_escalated"] + ["no_issue"] * (len(self.bench.series.lots) - 2)
                         + ["review_requested"])
        self.assertEqual([(s["role"], s["outcome"], s["subject"]) for s in prepare["steps"][len(supplier):]],
                         [("carbon", "calculated", b) for b in self.bench.series.batteries]
                         + [("issuance", "published", b) for b in series.clean])
        self.assertEqual(prepare["checkpoints"][-1]["observed"]["open_cases"], [series.case_lot])
        for step in prepare["steps"]:
            self.assertEqual(step["tool_reads"], list(SECTIONS[step["role"]]))
        self.assertEqual(prepare["evidence_level"], "local")
        review = results["review"][1].data
        self.assertEqual(review["steps"], [])
        self.assertEqual(review["checkpoints"][-1]["observed"],
                         {"published": sorted(series.clean), "open_reviews": [], "open_cases": []})
        self.assertEqual([d["observed"] for d in review["denials"]], ["403", "403", "409 compliance_case_open"])
        self.assertEqual([h["what"].split(" on ")[0] for h in review["human"][1:]],
                         ["Compliance recorded keep_hold", "Compliance recorded accept_with_action_plan"])
        complete = results["complete"][1].data
        self.assertEqual(sorted((s["role"], s["subject"]) for s in complete["steps"]),
                         sorted([("issuance", b) for b in series.held + series.compliance_held]
                                + [("supplier", series.lots[0]), ("supplier", series.case_lot)]))
        aftercare = results["aftercare"][1].data
        self.assertEqual({d["result"] for d in aftercare["denials"]}, {"PASS"})
        b3, b5, successor = series.batteries[2], series.batteries[4], series.successor
        self.assertEqual(aftercare["lifecycle"], {**{b: "active" for b in series.batteries}, b3: "repurposed",
                                                  b5: "recycled", successor: "active"})
        self.assertEqual([(s["subject"], s["action"]) for s in aftercare["steps"]],
                         [(b3, "record_soh"), (b3, "record_soh"), (b3, "record_ownership_change"),
                          (b3, "request_repurpose"), (b5, "record_soh"), (b5, "record_end_of_life"),
                          (b5, "record_recycling_closure")])
        links = next(c for c in aftercare["checkpoints"] if c["name"].startswith("second life approved"))
        self.assertEqual(links["observed"], {"successor": [successor], "predecessor": [b3]})
        observed = {d["name"]: d["observed"] for d in aftercare["denials"]}
        self.assertEqual([observed[name] for name in (
            "lifecycle event older than the last recorded one", "event on a closed passport",
            "event the lifecycle state does not allow", "the same signed event sent again",
            "Lifecycle agent identity decides a second-life request")],
            ["202 event_out_of_order", "202 passport_closed", "202 state_not_allowed", "201 routed (replayed)", "403"])
        state = self.bench.app.service.restricted_passport(b3)["state"]
        self.assertEqual((state["committed_version"], state["hold_reason"]), (5, domain.PASSPORT_CLOSED))
        checkpoints = {c["name"]: c for c in aftercare["checkpoints"]}
        ledger = checkpoints["SQL ledger verification (sp_verify_database_ledger)"]
        self.assertIn("unavailable", ledger["observed"][self.bench.series.clean[0]])
        views = checkpoints["field groups per viewer role"]
        self.assertEqual(views["observed"], {
            "public": ["identity", "footprint"],
            "OEM": ["identity", "footprint", "performance", "second_life", "warranty", "recall"],
            "Recycler": ["identity", "footprint", "performance", "composition"],
            "Authority": ["identity", "footprint", "performance", "composition", "calculation", "supply_chain",
                          "due_diligence", "ledger", "decisions"]})
        self.assertEqual(checkpoints["measures after aftercare (successor not a produced battery)"]["observed"],
                         {"produced": 5, "published": 5, "repurposed": 1})
        self.assertEqual([m["when"] for m in complete["measures"] + aftercare["measures"]],
                         ["after complete", "after aftercare"])
        self.assertEqual(aftercare["role_views"]["views"]["OEM"],
                         "identity, footprint, performance, second_life, warranty, recall")
        self.assertIn("Azure-only view", aftercare["workbook"])
        markdown = (self.bench.root / "runs" / f"{results['aftercare'][1].run_id}.md").read_text(encoding="utf-8")
        for heading in ("## Lifecycle status", f"## Role views of {successor}",
                        "## Success measures after aftercare", "## Executive workbook"):
            self.assertIn(heading, markdown)
        held = aftercare["paths"][self.bench.series.held[0]]
        clean = aftercare["paths"][self.bench.series.clean[0]]
        self.assertTrue(any(p.startswith("human Reviewer resolved") for p in held))
        cobalt = aftercare["paths"][series.compliance_held[0]]
        self.assertTrue(any(p.startswith("human Compliance decided") for p in cobalt))
        self.assertFalse(any(p.startswith("human") for p in clean))
        markdown = (self.bench.root / "runs" / f"{results['aftercare'][1].run_id}.md").read_text(encoding="utf-8")
        self.assertIn("Synthetic data - not a legal passport", markdown)
        self.assertIn("scripted double (not AI)", markdown)
        self.assert_reporting_parity()

    def assert_reporting_parity(self):
        """The Fabric measure tables computed from the app's export v2 equal the app's own series snapshot."""
        service = self.bench.app.service
        export = service.report_export()
        tables = measure_tables(export["rows"], export["lot_claims"],
                                {"export_id": export["export_id"], "generated_utc": export["generated_utc"]})
        snapshot = exec_views.series_snapshot(service)
        [success] = tables["success_measures"]
        keys = ("coverage_pct", "verified_footprint_batteries", "supplier_completeness_pct", "second_life_count",
                "second_life_value_eur_illustrative")
        self.assertEqual({key: success[key] for key in keys}, {key: snapshot[key] for key in keys})
        self.assertEqual((success["produced"], success["published"], success["second_life_count"]), (5, 5, 1))
        self.assertGreater(success["second_life_value_eur_illustrative"], 0)
        self.assertEqual([{key: row[key] for key in snapshot["plants"][0]} for row in tables["measures_by_plant"]],
                         snapshot["plants"])
        self.assertEqual(sum(row["second_life_count"] for row in tables["measures_by_plant"]), 1)
        self.assertEqual(sum(row["lots"] for row in tables["supplier_completeness"]), success["lots"])

    def test_reruns_replay_without_new_agent_work_or_duplicate_events(self):
        self.bench.run("prepare")
        again = self.bench.run("prepare")["prepare"]
        self.assertEqual(again[0], PASS)
        self.assertEqual(again[1].data["steps"], [])
        self.bench.run("review", "complete", "aftercare")
        self.assertEqual(self.bench.run("review")["review"][0], PASS)
        aftercare = self.bench.run("aftercare")["aftercare"][1].data
        self.assertEqual([s["outcome"] for s in aftercare["steps"]], ["already_recorded"] * 7)
        self.assertEqual({d["result"] for d in aftercare["denials"]}, {"PASS"})
        versions = {b: self.bench.view().state(b)["committed_version"] for b in self.bench.series.batteries}
        self.assertEqual(list(versions.values()), [1, 1, 5, 1, 4])
        self.assertEqual(len(self.bench.app.service.list_second_life()), 1)
        verdict, register = self.bench.run("prepare")["prepare"]
        self.assertEqual(verdict, STOPPED)
        self.assertIn("already complete", register.data["message"])

    def test_no_typed_confirmation_leaves_the_review_open_and_publishes_nothing_more(self):
        self.bench.run("prepare")
        runner = self.bench.runner("review")
        runner.confirm = lambda _: ""
        self.assertEqual(runner.execute("review"), STOPPED)
        view = self.bench.view()
        self.assertIsNotNone(view.open_review(self.bench.series.lots[0]))
        self.assertIsNotNone(view.open_case(self.bench.series.case_lot))
        self.assertEqual(view.committed_count(), 2)

    def test_complete_before_the_human_decision_stops(self):
        self.bench.run("prepare")
        self.assertEqual(self.bench.run(("complete", {}))["complete"][0], STOPPED)
        self.assertEqual(self.bench.view().committed_count(), 2)

    def next_step(self):
        series, lines = self.bench.series, []
        self.bench.app.runner(series, Register(series, "status", "local", out_dir=self.bench.root / "runs"),
                              lines.append).show_status()
        return lines[-1]

    def test_a_hold_without_the_corrected_claim_points_to_prepare_instead_of_the_reviewer(self):
        series = self.bench.series
        correction = simulator.correction_message(series.tag)
        prepare = self.bench.runner("prepare")
        send = prepare.send
        prepare.send = lambda envelope, what: None if envelope["payload"]["message_id"] == correction \
            else send(envelope, what)
        self.assertEqual(prepare.execute("prepare"), PASS)
        self.assertIn("run prepare: it sends the supplier's corrected claim", self.next_step())
        complete = self.bench.runner("complete", sleep=lambda _: self.fail("complete waited for an undecidable hold"))
        self.assertEqual(complete.execute("complete", wait=60), STOPPED)
        self.assertIn("run prepare: it sends the supplier's corrected claim", complete.register.data["message"])
        self.assertEqual(self.bench.run("prepare")["prepare"][0], PASS)
        self.assertIn("Human decision on", self.next_step())

    def test_a_hold_citing_every_document_names_the_reviewer_upload_that_unblocks_it(self):
        """A Supplier sourcing review on the accepted cobalt lot cites every document on file, so the hold page
        offers no decision until a Reviewer uploads a newer document."""
        self.bench.run("prepare", "review")
        app, lot = self.bench.app, self.bench.series.case_lot
        app.double.force_action = "request_review"
        try:
            opened = app.staff.post("/agents/supplier/run", json={"task_id": "test-case-lot-hold", "subject": lot})
        finally:
            app.double.force_action = None
        self.assertLess(opened.status_code, 400, opened.text)
        review = self.bench.view().open_review(lot)
        self.assertNotIn('name="evidence_id"', app.reviewer.get(f"/holds/{review['review_id']}").text)
        self.assertIn(f"upload a document for {lot}", self.next_step())
        complete = self.bench.runner("complete", sleep=lambda _: self.fail("complete waited for an undecidable hold"))
        self.assertEqual(complete.execute("complete", wait=60), STOPPED)
        self.assertIn(f"upload a document for {lot}", complete.register.data["message"])
        note = app.reviewer.post("/evidence", data={"kind": "other", "lot_id": lot, "command_id": "test-note"},
                                 files={"file": ("reviewer-note.json", b'{"note": "The audit answers the report."}',
                                                 "application/json")})
        self.assertLess(note.status_code, 400, note.text)
        self.assertIn('name="evidence_id"', app.reviewer.get(f"/holds/{review['review_id']}").text)
        resolved = app.reviewer.post(f"/reviews/{review['review_id']}/resolve", json={
            "expected_row_version": review["row_version"], "reason": "The site audit answers the adverse report.",
            "evidence_ids": [note.json()["evidence_id"]], "command_id": "test-resolve-case-lot"})
        self.assertLess(resolved.status_code, 400, resolved.text)
        self.assertEqual(self.bench.run("complete")["complete"][0], PASS)

    def test_the_largest_agent_context_of_the_story_fits_the_8000_byte_record_and_reports_16_kb(self):
        self.bench.run("prepare")
        agents = AgentService(self.bench.app.service)
        sizes = {}
        with self.bench.app.service.store.read() as tx:
            lots = {row["lot_id"] for row in tx.select("PackageLots") if row["material"] in domain.CRITICAL}
            batteries = [row["battery_id"] for row in tx.select("PassportState")]
            for role, subjects in (("supplier", lots), ("carbon", batteries), ("issuance", batteries)):
                sizes[role] = max(len(json.dumps(agents.context(tx, role, subject), sort_keys=True))
                                  for subject in subjects)
        self.assertLessEqual(set(self.bench.series.lots), lots)
        self.assertEqual(len(batteries), 5)
        self.assertLessEqual(max(sizes.values()), 8000, sizes)
        with self.bench.app.service.store.read() as tx:
            supplier = agents.context(tx, "supplier", self.bench.series.case_lot)
        self.assertEqual(sorted(e["kind"] for e in supplier["evidence"]), ["adverse_report", "supplier_claim"])
        report = next(e for e in supplier["evidence"] if e["kind"] == "adverse_report")
        self.assertLessEqual(len(simulator.evidence_bytes(report["content"])), 16 * 1024)
        self.assertLessEqual(len(SECTIONS["supplier"]), 8)

    def test_a_larger_second_series_counts_only_its_own_batteries(self):
        self.bench.run("prepare", "review", "complete")
        other = Bench("T2", extra=2, app=self.bench.app, root=self.bench.root / "t2")
        results = other.run("prepare", "review", "complete", "aftercare")
        self.assertEqual({verdict for verdict, _ in results.values()}, {PASS})
        self.assertEqual(len(results["prepare"][1].data["steps"]), len(other.series.lots) + 7 + 4)
        self.assertEqual(other.view().committed_count(), 7)
        self.assertEqual(self.bench.view().committed_count(), 5)


class SurpriseTests(unittest.TestCase):
    def test_a_deferring_agent_stops_the_run_without_publishing(self):
        bench = Bench(double=ScriptedDouble(defer_roles={"carbon"}))
        self.addCleanup(bench.close)
        verdict, register = bench.run("prepare")["prepare"]
        self.assertEqual(verdict, STOPPED)
        self.assertIn("no progress", register.data["message"])
        self.assertEqual(bench.view().committed_count(), 0)

    def test_an_agent_that_misses_the_conflict_fails_the_story(self):
        bench = Bench(double=ScriptedDouble(agree_all=True))
        self.addCleanup(bench.close)
        verdict, register = bench.run("prepare")["prepare"]
        self.assertEqual(verdict, FAIL)
        self.assertIn("expected review_requested", register.data["message"])
        self.assertEqual(bench.view().committed_count(), 0)


class VariedSeriesTests(unittest.TestCase):
    """Added batteries with seeded data issues: the acts route each issue to the pipeline, a Reviewer or Compliance,
    record every agent answer against the expected one, and continue when an answer differs."""

    def play(self, double=None):
        bench = Bench("S3", extra=8, double=double)
        self.addCleanup(bench.close)
        return bench, bench.run("prepare", "review", "complete", "aftercare")

    def markdown(self, bench, register):
        return (bench.root / "runs" / f"{register.run_id}.md").read_text(encoding="utf-8")

    def test_seeded_issues_go_to_the_pipeline_or_people_and_open_ones_stay_held(self):
        bench, results = self.play()
        series = bench.series
        self.assertEqual(sorted(series.issues.values()), sorted(simulator.ISSUES))
        self.assertEqual({act: verdict for act, (verdict, _) in results.items()},
                         {"prepare": PASS, "review": PASS, "complete": PASS, "aftercare": PASS})
        for _, register in results.values():
            self.assertEqual([c["name"] for c in register.data["checkpoints"] if not c["ok"]], [])
            self.assertEqual(register.deviations(), [])
        prepare = results["prepare"][1].data
        self.assertEqual(prepare["seeded_issues"], {kind: 1 for kind in simulator.ISSUES})
        self.assertEqual(len(prepare["accuracy"]), len([s for s in prepare["steps"] if s["role"] in
                                                        ("supplier", "carbon", "issuance")]))
        view = bench.view()
        published = {b for b in series.batteries if view.committed(b)}
        self.assertEqual(published, set(series.final_published))
        self.assertEqual(len(published), 9)
        self.assertEqual({b for b in series.blocked if view.blocked(b)}, set(series.blocked))
        self.assertEqual([lot for lot in series.open_lots if view.open_review(lot) or view.open_case(lot)],
                         list(series.open_lots))
        self.assertEqual([d["observed"] for d in results["review"][1].data["denials"]],
                         ["403", "403", "409 compliance_case_open"])
        markdown = self.markdown(bench, results["prepare"][1])
        self.assertIn("## Expected vs actual agent outcomes", markdown)
        self.assertIn("### Subjects with a seeded issue", markdown)
        self.assertNotIn("### Different from expected", markdown)
        self.assertIn("Agent answers as expected", results["complete"][1].data["message"])
        measures = next(c for c in results["aftercare"][1].data["checkpoints"] if c["name"].startswith("measures"))
        self.assertEqual(measures["observed"]["produced"], len(series.batteries))

    def test_an_agent_that_misses_seeded_issues_is_recorded_and_the_run_continues(self):
        bench, results = self.play(ScriptedDouble(agree_all=True))
        self.assertEqual({verdict for verdict, _ in results.values()}, {PASS})
        prepare = results["prepare"][1]
        deviations = prepare.deviations()
        self.assertTrue(deviations)
        self.assertEqual({(d["role"], d["actual"]) for d in deviations if d["role"] == "supplier"},
                         {("supplier", "no_issue")})
        self.assertIn("### Different from expected", self.markdown(bench, prepare))
        self.assertIn("checkpoints that differed: 1", prepare.data["message"])

    def test_the_story_refuses_a_series_with_seeded_issues(self):
        bench = Bench("S3", extra=8)
        self.addCleanup(bench.close)
        runner = bench.runner("story")
        self.assertEqual(runner.execute("story"), STOPPED)
        self.assertIn("Run prepare, review --yes and complete instead", runner.register.data["message"])
        self.assertEqual(bench.view().committed_count(), 0)


class PreviewTests(unittest.TestCase):
    def test_a_decision_on_the_hold_screen_lets_complete_wait_continue(self):
        bench = Bench("T3")
        self.addCleanup(bench.close)
        self.assertEqual(bench.run("prepare")["prepare"][0], PASS)
        lot = bench.series.lots[0]
        review = bench.view().open_review(lot)
        case = bench.view().open_case(bench.series.case_lot)
        correction = next(e["evidence_id"] for e in bench.view().lot_evidence(lot) if e["kind"] == "corrected_claim")
        audit = bench.runner("review").upload(bench.series.audit_evidence())
        compliance = {LOCAL_USER_HEADER: "series-compliance", LOCAL_ROLES_HEADER: "Passport.Compliance"}
        decisions = []

        def form_of(path, headers):
            page = bench.app.client.get(path, headers=headers).text
            return {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)
                    for name in ("command_id", "expected_row_version")}

        def decide(_seconds):
            if not decisions:
                form = form_of(f"/holds/{review['review_id']}", REVIEWER)
                decisions.append(bench.app.client.post(
                    f"/holds/{review['review_id']}/resolve", headers=REVIEWER, follow_redirects=False,
                    data={**form, "evidence_id": correction, "reason": "Corrected claim matches the transport record"},
                ).status_code)
            elif len(decisions) == 1:
                form = form_of(f"/cases/{case['case_id']}", compliance)
                decisions.append(bench.app.client.post(
                    f"/cases/{case['case_id']}/decide", headers=compliance, follow_redirects=False,
                    data={**form, "decision": "accept_with_action_plan", "evidence_id": audit,
                          "owner": "Supplier quality lead", "due_date": "2027-01-31",
                          "reason": "The site audit answers the adverse report."}).status_code)

        verdict = bench.runner("complete", sleep=decide).execute("complete", wait=60)
        self.assertEqual((decisions, verdict, bench.view().committed_count()), ([303, 303], PASS, 5))

    def test_the_preview_persona_replaces_caller_identity_and_redirects_only_locally(self):
        from fastapi.testclient import TestClient
        user, roles = (name.decode() for name in preview.IDENTITY)

        async def echo(scope, receive, send):
            body = json.dumps({k.decode(): v.decode() for k, v in scope["headers"] if k.lower() in preview.IDENTITY})
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body.encode()})

        client = TestClient(preview.Personas(echo))
        self.assertEqual(client.get("/exec", headers={roles: "Passport.Operator,Passport.Reviewer"}).json(),
                         {user: "preview-presenter", roles: preview.PERSONAS["presenter"][1]})
        local = client.get("/preview/as/operator?next=/trail/B-001", follow_redirects=False)
        self.assertEqual((local.status_code, local.headers["location"]), (303, "/trail/B-001"))
        away = client.get("/preview/as/anonymous?next=//evil.example", follow_redirects=False)
        self.assertEqual(away.headers["location"], "/exec")
        self.assertEqual(client.get("/exec", headers={user: "x", roles: "Passport.Reviewer"}).json(), {})
        self.assertEqual(client.get("/preview/as/superuser").status_code, 404)
        client.get("/preview/as/admin")
        self.assertEqual(client.get("/settings").json()[roles], "Passport.Operator,Passport.Admin")

    def test_preview_runs_act_one_then_serves_the_chosen_persona(self):
        lines, served = [], []

        def serve(asgi, port):
            served.append((type(asgi), asgi.default, port))
            deadline = time.monotonic() + 30  # let the background complete act finish before the app closes
            while sum(line.startswith("Evidence register:") for line in lines) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)

        self.assertEqual(preview.preview("T4", port=8123, persona="operator", wait=0, out=lines.append, serve=serve),
                         0)
        self.assertEqual(served, [(preview.Personas, "operator", 8123)])
        self.assertIn("PASS", [line.split(":")[0] for line in lines])
        self.assertTrue(any(line.startswith("STOPPED: the human decision") for line in lines))
        self.assertTrue(any("scripted double, not AI" in line for line in lines))
        self.assertEqual(preview.preview(persona="superuser", out=lines.append), 1)
        self.assertEqual(acts.PERSONAS, tuple(preview.PERSONAS))


class JourneyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.bench = Bench()
        self.addCleanup(self.bench.close)
        self.client = TestClient(self.bench.app.client.app)
        self.addCleanup(self.client.close)
        self.client.headers.update({LOCAL_USER_HEADER: "journey-user",
                                    LOCAL_ROLES_HEADER: ",".join(sorted(experience.HUMAN_ROLES)),
                                    "accept": "text/html"})
        self.assertEqual(self.bench.run("prepare")["prepare"][0], PASS)
        self.persona = None

    def choose(self, profile):
        response = self.client.post("/personas", data={"persona": profile, "return_to": "/journey"})
        self.assertEqual(response.status_code, 200)
        self.persona = profile

    def step(self, key):
        return self.client.post("/journey", data={"persona": self.persona, "step": key,
                                                "series": self.bench.series.series_id})

    def test_every_stop_uses_real_records_without_executing_the_story(self):
        with self.bench.app.service.store.read() as tx:
            before = tx.tables
        for profile, stops in experience.TOURS.items():
            self.choose(profile)
            expected_roles = [experience.PROFILES[profile][1]] if experience.PROFILES[profile][1] else []
            for index, stop in enumerate(stops, 1):
                response = self.step(stop.key)
                self.assertEqual(response.status_code, 200, stop.key)
                self.assertIn(f"Stop {index} of {len(stops)}", response.text)
                self.assertIn('id="journey-title"', response.text, stop.key)
                me = self.client.get("/me").json()
                self.assertEqual(me["user_id"], "local:journey-user")
                self.assertEqual(me["roles"], expected_roles, stop.key)
                if profile == "public":
                    self.assertNotIn('aria-labelledby="sec-performance"', response.text)
                if stop.key == "oem":
                    self.assertIn('aria-labelledby="sec-performance"', response.text)
                    self.assertNotIn('aria-labelledby="sec-composition"', response.text)
                if stop.key == "recycler":
                    self.assertIn('aria-labelledby="sec-composition"', response.text)
                if stop.key in ("second-life", "successor"):
                    self.assertIn("No second-life request yet", response.text)
        with self.bench.app.service.store.read() as tx:
            self.assertEqual(tx.tables, before)

    def test_compliance_upload_returns_to_case_and_does_not_decide_it(self):
        self.choose("compliance")
        page = self.step("compliance")
        case_url = page.url.path
        self.assertIn("Upload audit report", page.text)
        audit = self.bench.series.audit_evidence()
        response = self.client.post("/evidence",
                                    data={"kind": audit.kind, "lot_id": audit.lot,
                                          "command_id": "journey-audit", "return_to": case_url},
                                    files={"file": (audit.name, audit.data, "application/json")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url.path, case_url)
        self.assertIn("Evidence request accepted", response.text)
        self.assertIn(audit.name, response.text)
        self.assertIsNotNone(self.bench.view().open_case(audit.lot))
        self.assertEqual(self.bench.view().committed_count(), 2)

    def test_readiness_keeps_the_compliance_decision_visible_after_sourcing_review(self):
        self.choose("operator")
        self.bench.runner("review").resolve_review(self.bench.series.lots[0],
                                                   "Checked the corrected source against transport evidence.", yes=True)
        page = self.step("outcome")
        self.assertIn("A sourcing risk still needs a human decision", page.text)
        self.assertIn("0 sourcing review(s), 1 compliance case(s)", page.text)
        self.assertIn("Claim only (1 of 2)", page.text)
        self.assertIn("Switch to Compliance Officer", page.text)
        view = exec_views.series_view(self.bench.app.service, self.bench.series.series_id)
        self.assertEqual(view["cases"][0]["batteries"], list(self.bench.series.compliance_held))
        self.assertEqual(view["action"]["url"], view["cases"][0]["url"])
        opened = self.client.post("/personas", data={"persona": "compliance", "return_to": view["action"]["url"]})
        self.assertEqual(opened.status_code, 200)
        self.assertIn("Your decision", opened.text)

    def test_aftercare_waits_for_explicit_browser_approval_and_never_approves_on_timeout(self):
        self.choose("reviewer")
        self.bench.run("review", "complete")
        runner = self.bench.runner("aftercare")
        self.assertEqual(runner.execute("aftercare", wait=1), STOPPED)
        request = self.bench.app.service.list_second_life()[-1]
        self.assertEqual(request["status"], "pending")
        self.assertIsNone(self.bench.app.service.public_passport(self.bench.series.successor))
        page = self.step("second-life")
        self.assertIn("Your decision", page.text)
        data = {"decision": "approve", "reason": "Reviewed the synthetic safety reference and state of health.",
                "expected_row_version": request["row_version"], "command_id": "journey-second-life"}
        decided = self.client.post(f"/second-life/{request['request_id']}/decision", data=data)
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(self.bench.runner("aftercare").execute("aftercare", wait=1), PASS)
        self.choose("oem")
        successor = self.step("successor")
        self.assertEqual(successor.url.path, f"/p/{self.bench.series.successor}")
        self.assertIn("Predecessor", successor.text)
        self.choose("recycler")
        self.assertIn("recycled", self.step("recycling").text)

    def test_compliance_only_identity_completes_its_tour_without_staff_permissions(self):
        self.client.headers[LOCAL_ROLES_HEADER] = "Passport.Compliance"
        self.persona = "compliance"
        for index, key in enumerate(("case-context", "case-evidence", "compliance", "case-history"), 1):
            page = self.step(key)
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.url.path.startswith("/cases/"))
            self.assertIn(f"Stop {index} of 4", page.text)
        self.assertEqual(self.client.get("/exec").status_code, 403)
        self.assertEqual(self.client.get("/me").json()["roles"], ["Passport.Compliance"])
        self.assertEqual(self.bench.view().committed_count(), 2)

    def test_policy_tour_does_not_require_a_production_series(self):
        self.client.headers[LOCAL_ROLES_HEADER] = "Passport.PolicyApprover"
        for key in ("policy-active", "policy"):
            page = self.client.post("/journey", data={"persona": "policy", "step": key})
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.url.path, "/policy")
            self.assertIn("Policy Approver tour", page.text)


class ModelLikeDouble(ScriptedDouble):
    """The scripted rules reported as a model run, so the app treats their reasons as AI-generated."""

    METADATA = {**ScriptedDouble.METADATA, "hosting": "foundry_prompt", "model": "test-model", "model_version": "1"}


class ReasonOriginTests(unittest.TestCase):
    def test_the_register_keeps_agent_reasons_apart_and_marks_only_ai_text(self):
        for double, ai in ((ScriptedDouble(), False), (ModelLikeDouble(), True)):
            with self.subTest(ai=ai):
                bench = Bench(double=double)
                self.addCleanup(bench.close)
                verdict, register = bench.run("prepare")["prepare"]
                self.assertEqual(verdict, PASS)
                held = next(s for s in register.data["steps"] if s["outcome"] == "review_requested")
                self.assertTrue(held["agent_reason"])
                self.assertIs(held["agent_reason_ai"], ai)
                self.assertNotIn(held["agent_reason"], held["app_message"])
                found = bench.app.viewers["Authority"].post("/provenance/verify",
                                                            json={"text": held["agent_reason"]}).json()
                self.assertIn(held["provenance_id"], [r["provenance_id"] for r in found["records"]])
                self.assertEqual({r["ai_generated"] for r in found["records"]}, {ai})
                markdown = (bench.root / "runs" / f"{register.run_id}.md").read_text(encoding="utf-8")
                self.assertEqual(f"*(AI-generated)* {held['agent_reason']}" in markdown, ai)


class RegisterTests(unittest.TestCase):
    def test_redaction_and_cost_scope(self):
        self.assertEqual(redact("user a.b@contoso.com id 00000000-0000-0000-0000-0000000000f1 at "
                                "x.y.azurecontainerapps.io"),
                         "user [account-redacted] id [id-redacted] at [host-redacted]")
        with tempfile.TemporaryDirectory() as tmp:
            series = Series("T1", output=Path(tmp))
            prices = {"currency": "USD", "source": "test", "models": {"m": {"input_per_million": 2,
                                                                             "output_per_million": 8}}}
            register = Register(series, "prepare", "tenant", prices=prices, out_dir=Path(tmp))
            row = {"subject": "B-001", "http_status": 200, "wall_ms": 5}
            register.step({**row, "role": "issuance", "model": "m", "input_tokens": 500_000,
                           "output_tokens": 250_000, "outcome": "published"})
            register.step({**row, "role": "carbon", "model": "unknown", "input_tokens": 1, "output_tokens": 1,
                           "outcome": "calculated"})
            cost = json.loads(register.finish(PASS, "ok").read_text(encoding="utf-8"))["cost"]
            self.assertEqual((cost["model_token_cost"], cost["per_published_passport"], cost["unpriced_agent_calls"]),
                             (3.0, 3.0, 1))
            self.assertEqual(cost["status"], "partially measured")
            self.assertIn("excludes hosting", cost["scope"])
            unpriced = Register(series, "review", "tenant", out_dir=Path(tmp))
            cost = json.loads(unpriced.finish(PASS, "ok").read_text(encoding="utf-8"))["cost"]
            self.assertEqual(cost["status"], "not measured")
            self.assertIn("prices_file", cost["note"])

    def test_evidence_command_ids_match_the_deployment_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            series = Series("T1", output=Path(tmp))
            items = series.opening_evidence()
            for item in items:
                (Path(tmp) / item.name).write_bytes(item.data)
            sent = []

            def post(url, fields, file, token):
                sent.append(fields["command_id"])
                return 201, '{"evidence_id": "e"}'

            supply.upload_evidence(post, APP_URL, TOKEN, tmp, [(i.name, i.kind, i.lot) for i in items])
        self.assertEqual(sent, [item.command_id for item in items])


# ---- the acts against the "deployed" app ------------------------------------------------------------------------

class Network:
    """HTTPS to the deployed app: URLs below APP_URL reach the in-memory app; TOKEN is the operator's token."""

    def __init__(self, app):
        self.app, self.down = app, False

    def send(self, method, url, token, **kwargs):
        if self.down:
            return None, "connection refused"
        if not url.startswith(APP_URL):
            raise AssertionError(f"unexpected URL {url}")
        client = self.app.anonymous if token is None else self.app.staff if token == TOKEN else None
        if client is None:
            return 401, json.dumps({"error": "unauthenticated"})
        response = client.request(method, url[len(APP_URL):], **kwargs)
        return response.status_code, response.text

    def get(self, url):
        return self.send("GET", url, None)

    def get_auth(self, url, token):
        return self.send("GET", url, token)

    def post(self, url, body, token):
        return self.send("POST", url, token, json=body)

    def post_multipart(self, url, fields, file, token):
        field, name, content_type, data = file
        return self.send("POST", url, token, data=fields, files={field: (name, data, content_type)})


class Intake:
    """The app storage account's intake container, as the Azure SDK container client presents it."""

    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=name) for name in self.blobs.list(supply.INTAKE, name_starts_with)]

    def download_blob(self, name):
        data = self.blobs.get(supply.INTAKE, name)
        return SimpleNamespace(readall=lambda: data)

    def upload_blob(self, name, data, overwrite=False):
        self.blobs.put(supply.INTAKE, name, data, "application/json", overwrite=overwrite)


class TenantTests(Workspace):
    def setUp(self):
        super().setUp()
        folder = self.state.path.parent
        self.output = folder / "output"
        self.app = LocalApp(folder / "app", ScriptedDouble())
        self.addCleanup(self.app.close)
        # The deployed app reads this registry from DBP_PARTY_REGISTRY_B64, which B3 writes from the same keys.
        self.app.client.app.state.connector.sync(simulator.party_registry(simulator.signing_keys(self.output)))
        self.network = Network(self.app)
        self.fabric = FakeFabric(folder / "onelake")
        self.az = FakeAzure(Capacity().routes())
        self.runtime = deploy.Runtime(
            http_get=self.network.get, http_get_auth=self.network.get_auth, http_post=self.network.post,
            http_post_multipart=self.network.post_multipart, sleep=lambda _: None, output=self.output,
            fabric_send=self.fabric,
            container=lambda url, name: contextlib.nullcontext(Intake(self.app.service.blobs)))
        self.token = self.enterContext(mock.patch.object(smoke, "app_token", return_value=TOKEN))
        self.enterContext(mock.patch.object(deploy, "_command", return_value={"appStorageUrl": "https://x",
                                                                            "fabricCapacityId": CAPACITY}))
        self.config = make_config()
        self.state.record("B1", "fp", {"app_id": APP_ID})
        self.state.record("B3", "fp", {"appUrl": APP_URL})
        self.state.record("B4", "fp", {"agent_mode": True})

    def act(self, act, **options):
        return acts.run(self.config, self.az, self.state, act, runtime=self.runtime, out=self.out, **options)

    def register(self, series, act):
        return json.loads(max((self.output / "runs").glob(f"*-{series}-{act}.json")).read_text(encoding="utf-8"))

    def test_the_main_series_runs_only_after_deploy_imported_it(self):
        self.assertEqual(self.act("prepare"), STOPPED)
        self.assertIn("not imported with the current settings", self.printed)
        supply.seed_app(self.config, None, APP_URL, APP_ID, Intake(self.app.service.blobs), self.network.post,
                        self.network.post_multipart, lambda _: None, self.output, quiet)
        prepared = supply.prepare(self.config, output=self.output)
        self.state.record("B6", deploy.seed_fingerprint(self.config, APP_ID, APP_URL, prepared, self.output), {})
        self.lines.clear()

        self.assertEqual(self.act("prepare"), PASS)
        self.assertIn(acts.PUBLISH_HINT, self.printed)
        register = self.register("MAIN", "prepare")
        self.assertEqual((register["evidence_level"], register["batteries"]), ("tenant observed", 5))
        self.assertEqual(register["cost"]["status"], "not measured")
        self.config = make_config(deploy_fabric=False)
        self.lines.clear()
        # This different deployment mode must have its own completed B6.
        self.assertEqual(self.act("prepare"), STOPPED)
        self.state.record("B6", deploy.seed_fingerprint(self.config, APP_ID, APP_URL, prepared, self.output), {})
        self.assertEqual(self.act("prepare"), PASS)
        self.assertNotIn(acts.PUBLISH_HINT, self.printed)

    def test_a_tagged_series_runs_every_act_and_fails_only_at_the_missing_sql_ledger(self):
        self.assertEqual(self.act("prepare", tag="T5"), PASS)
        self.assertEqual(self.act("review", tag="T5", interactive=False), STOPPED)
        self.assertIn("run it in a terminal to confirm, or add --yes", self.printed)
        self.assertEqual(self.act("review", tag="T5", yes=True), PASS)
        self.assertEqual(self.act("complete", tag="T5"), PASS)
        self.assertIn("passport coverage           5 of 5 produced batteries published", self.printed)
        self.lines.clear()
        self.assertEqual(self.act("aftercare", tag="T5"), FAIL)
        self.assertIn("OEM        not exercised - needs a test user with Passport.OEM "
                      "(python -m dbp_mvp roles assign)", self.printed)
        aftercare = self.register("T5", "aftercare")
        self.assertIn("SQL ledger verification did not pass", aftercare["message"])
        self.assertEqual({d["result"] for d in aftercare["denials"]}, {"PASS", "not exercised"})
        self.assertNotIn(acts.PUBLISH_HINT, self.printed)
        self.lines.clear()
        with mock.patch.object(Runner, "ledger_check", return_value=None):
            self.assertEqual(self.act("aftercare", tag="T5"), PASS)
        self.assertIn("executive workbook: link not known", self.printed)
        self.assertIn(acts.PUBLISH_HINT, self.printed)
        self.lines.clear()
        self.assertEqual(self.act("status", tag="T5"), PASS)
        self.assertIn("Series T5 (SERIES-MAIN-T5): 5 of 5 passports published", self.printed)

    def test_the_story_plays_on_while_people_decide_in_the_app(self):
        series = Series("T8", output=self.output)
        client = self.app.client
        compliance = {LOCAL_USER_HEADER: "series-compliance", LOCAL_ROLES_HEADER: "Passport.Compliance"}
        decided = []

        def rows(path, key):
            return self.app.staff.get(path).json()[key]

        def submit(path, action, headers, **data):
            page = client.get(path, headers={**headers, "accept": "text/html"}).text
            form = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)
                    for name in ("command_id", "expected_row_version")}
            return client.post(f"{path}/{action}", headers=headers, follow_redirects=False,
                               data={**form, **data}).status_code

        def evidence(lot, kind):
            return next(e["evidence_id"] for e in rows("/evidence", "evidence")
                        if e["lot_id"] == lot and e["kind"] == kind)

        def decide(_seconds):
            """The people in the app: the hold first, then the case and the second-life request together."""
            review = next((r for r in rows("/reviews", "reviews")
                           if r["lot_id"] == series.lots[0] and r["status"] == "open"), None)
            if review and not decided:
                decided.append(submit(f"/holds/{review['review_id']}", "resolve", REVIEWER,
                                      evidence_id=evidence(series.lots[0], "corrected_claim"),
                                      reason="Corrected claim matches the transport record"))
                return
            case = next((c for c in rows("/compliance/cases", "cases")
                         if c["lot_id"] == series.case_lot and c["status"] == "open"), None)
            request = next((r for r in rows("/second-life", "requests") if r["status"] == "pending"), None)
            if case and request:
                decided.append(submit(f"/cases/{case['case_id']}", "decide", compliance,
                                      decision="accept_with_action_plan",
                                      evidence_id=evidence(series.case_lot, "audit_report"),
                                      owner="Supplier quality lead", due_date="2027-01-31",
                                      reason="The site audit answers the adverse report."))
                decided.append(submit(f"/second-life/{request['request_id']}", "decision", REVIEWER,
                                      decision="approve",
                                      reason="Safety assessment read; state of health meets the minimum."))

        self.runtime.sleep = decide
        with mock.patch.object(deploy, "ledger") as ledger, mock.patch.object(Runner, "ledger_check",
                                                                             return_value=None):
            self.assertEqual(self.act("story", tag="T8", wait=600), PASS)
        self.assertEqual(decided, [303, 303, 303])
        self.assertEqual([c.args[2] for c in ledger.call_args_list], ["anchor", "anchor"])
        self.assertIn(f"Passport readiness: {APP_URL}/exec?series={series.series_id}", self.printed)
        self.assertIn(f"{APP_URL}/second-life/", self.printed)
        self.assertIn("Leave this window running", self.printed)
        story = self.register("T8", "story")
        self.assertTrue(story["message"].startswith("Story complete."), story["message"])
        b3, b5 = series.batteries[2], series.batteries[4]
        self.assertEqual([(s["subject"], s["action"]) for s in story["steps"] if s["role"] == "lifecycle"],
                         [(b3, "record_soh"), (b3, "record_soh"), (b3, "record_ownership_change"),
                          (b3, "request_repurpose"), (b5, "record_soh"), (b5, "record_end_of_life"),
                          (b5, "record_recycling_closure")])
        self.assertEqual(story["lifecycle"][b5], "recycled")
        self.assertEqual(story["lifecycle"][series.successor], "active")
        self.lines.clear()
        self.assertEqual(self.act("story", tag="T8"), STOPPED)
        self.assertIn("has already played its whole story", self.printed)

    def test_aftercare_names_the_executive_workbook_only_when_the_deployment_reports_it(self):
        self.assertIn("link not known", acts._workbook(self.config, None, {"appStorageUrl": "https://x"}))
        ids = [{"persona": "agents", "id": "/subscriptions/s/wb-agents"},
               {"persona": "executive", "id": "/subscriptions/s/wb-exec"}]
        with mock.patch.object(acts.preflight, "session_check", return_value={"tenant_id": "contoso"}):
            line = acts._workbook(self.config, None, {"workbookIds": ids})
        self.assertTrue(line.endswith(": https://portal.azure.com/#@contoso/resource/subscriptions/s/wb-exec"), line)

    def test_refusals_name_the_missing_step(self):
        self.assertEqual(self.act("prepare", tag="T6"), PASS)
        self.config = make_config(extra_batteries=1)
        self.assertEqual(self.act("prepare", tag="T6", batteries=1), STOPPED)
        self.assertIn("Use a new --tag", self.printed)
        self.state.record("B4", "fp", {"agent_mode": False})
        with self.assertRaisesRegex(MvpError, "agents are not deployed"):
            self.act("prepare", tag="T6")
        self.assertEqual(self.act("status", tag="T6"), PASS)
        with self.assertRaisesRegex(MvpError, "no deployed passport app recorded"):
            acts.run(self.config, None, State(self.state.path.with_name("other.json")), "status",
                     runtime=self.runtime, out=self.out)

    def test_the_token_is_renewed_once_and_an_unreachable_app_stops_the_act(self):
        self.token.side_effect = ["expired", TOKEN]
        http = acts.Http(APP_URL, acts.Token(None, APP_ID), self.runtime)
        self.assertEqual(http.get("/reviews").status_code, 200)
        self.assertEqual(self.token.call_count, 2)
        with self.assertRaises(ValueError):
            acts.Http(APP_URL, None, self.runtime).post("/evidence", json={})
        self.network.down = True
        with self.assertRaises(NoAnswer):
            http.get("/reviews")
        self.token.side_effect = None
        self.assertEqual(self.act("prepare", tag="T7"), STOPPED)
        self.assertIn("the app did not answer (connection refused)", self.printed)

    def test_credential_negatives_reach_the_deployed_connector_without_a_revoked_party(self):
        supply.seed_app(self.config, None, APP_URL, APP_ID, Intake(self.app.service.blobs), self.network.post,
                        self.network.post_multipart, lambda _: None, self.output, quiet)
        config = make_config(scenarios={"evaluation": False, "credential_negatives": True})
        with mock.patch.object(deploy.preflight, "session_check"), \
                mock.patch.object(deploy, "OUTPUT", self.output):
            report = deploy.credential_negatives(config, None, self.state, apply=True, runtime=self.runtime,
                                                 out=self.out)
        results = {row["check"]: row["result"] for row in report["checks"]}
        self.assertEqual(results.pop("claim from a party whose key is revoked"), "not exercised")
        self.assertEqual(set(results.values()), {PASS})
        self.assertEqual(report["mode"], "deployed")
        self.assertIn("9 of 10 credential checks passed, 1 not exercised", self.printed)
        self.assertTrue((self.output / "credential_negatives" / "deployed" / "report.md").is_file())


class RunCommandTests(Workspace):
    def main(self, *argv):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["run", *argv])
        return code, errors.getvalue()

    def test_options_of_other_acts_and_bad_values_are_refused_before_anything_runs(self):
        with mock.patch.object(acts, "local") as local, mock.patch.object(acts, "run") as run, \
                mock.patch.object(cli, "load") as load:
            for argv, message in ((("prepare", "--batteries", "2"), "--batteries requires --tag"),
                                  (("local", "--wait", "5"), "--wait belongs to run complete, run aftercare and "
                                                             "run story."),
                                  (("story", "--reason", "Looks right"), "--reason belongs to run review."),
                                  (("story", "--skip-lifecycle"), "--skip-lifecycle belongs to run aftercare."),
                                  (("review", "--as", "admin"), "--as belongs to run preview."),
                                  (("aftercare", "--yes"), "--yes belongs to run review."),
                                  (("complete", "--wait", "86401"), "--wait must be between 0 and 86400 seconds."),
                                  (("preview", "--port", "80"), "--port must be between 1024 and 65535.")):
                with self.subTest(argv=argv):
                    code, errors = self.main(*argv)
                    self.assertEqual(code, 1)
                    self.assertIn(message, errors)
            self.assertFalse(local.called or run.called or load.called)
        for argv, message in ((("local", "--tag", "bad tag"), "--tag must be 1-6 capital letters"),
                              (("preview", "--batteries", "51"), "--batteries must be a whole number")):
            with self.subTest(argv=argv):
                code, errors = self.main(*argv)
                self.assertEqual(code, 1)
                self.assertIn(message, errors)

    def test_verdicts_map_to_exit_codes(self):
        with mock.patch.object(acts, "local", return_value=STOPPED) as local:
            self.assertEqual(self.main("preview", "--as", "admin", "--port", "8100")[0], 3)
        local.assert_called_once_with("preview", None, 0, 8100, "admin")
        with mock.patch.object(cli, "load", return_value=make_config()), mock.patch.object(cli, "Azure"), \
                mock.patch.object(cli, "State", return_value=self.state):
            for verdict, code in ((PASS, 0), (FAIL, 1), (STOPPED, 3)):
                with mock.patch.object(acts, "run", return_value=verdict) as run:
                    self.assertEqual(self.main("complete", "--tag", "T1", "--wait", "60")[0], code)
            self.assertEqual(run.call_args.kwargs, {"tag": "T1", "reason": None, "yes": False, "wait": 60,
                                                    "lifecycle": True, "batteries": None})
            with mock.patch.object(acts, "run", return_value=PASS) as run:
                self.assertEqual(self.main("prepare", "--tag", "SCALE1", "--batteries", "10")[0], 0)
                self.assertEqual(run.call_args.kwargs["batteries"], 10)
            with mock.patch.object(acts, "run", return_value=PASS) as run:
                self.assertEqual(self.main("story", "--wait", "7200")[0], 0)
        self.assertEqual((run.call_args.args[3], run.call_args.kwargs["wait"]), ("story", 7200))


if __name__ == "__main__":
    unittest.main()
