"""Executive screens: roles, held vs published views, the hold decision, the trail,
"not measured" handling, ledger results and minimized business events."""

import base64
import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import helpers
from helpers import REVIEWER
from passport import exec_views, experience, telemetry
from passport.agent_contracts import SECTIONS
from passport.agent_service import AgentService
from passport.config import Settings, check_prices, decode_b64_json
from passport.ledger import SqlLedger, build_ledger, digest_prefix, read_incarnation
from passport.web import create_app

OP = {"x-local-user": "olivia", "x-local-roles": "Passport.Operator"}
REV = {"x-local-user": "rafael", "x-local-roles": "Passport.Reviewer"}
NOBODY = {"x-local-user": "nora", "x-local-roles": ""}
COMP = {"x-local-user": "carmen", "x-local-roles": "Passport.Compliance"}
AUTH = {"x-local-user": "aurora", "x-local-roles": "Passport.Authority"}
AGENT = {"x-local-user": "supplier-agent", "x-local-roles": "Passport.Agent.Supplier"}
BANNER = "Synthetic data"
CLAIM = {"origin_site": "Mine North", "material": "lithium carbonate", "quantity_kg": 100}
TRANSPORT = {"loaded_at_site": "Mine South", "material": "lithium carbonate", "quantity_kg": 100}
CORRECTED = {"origin_site": "Mine South", "material": "lithium carbonate", "quantity_kg": 100}
HOLD_REASON = "Claim and transport record disagree on origin"
CHECKED = "2026-11-11T09:30:00Z"


def form_value(page, name):
    return re.search(rf'name="{name}" value="([^"]+)"', page).group(1)


class FakeLedger:
    def __init__(self, status="verified"):
        self.status, self.calls = status, []

    def check(self, battery_id=None, version=None):
        self.calls.append((battery_id, version))
        return {"status": self.status, "reason_code": "ok", "message": "fake", "checked_utc": CHECKED,
                "performed": True}

    def last(self, battery_id=None, version=None):
        return {"status": self.status, "checked_utc": CHECKED} if self.calls else None


class Screens(unittest.TestCase):
    """BUILD-03 with LOT-A on hold: BAT-103 published, BAT-101/102 held (no agent runs)."""

    def build(self, ledger=None, sink=None, **settings):
        self.service, self.blobs = helpers.new_service()
        helpers.put_package(self.blobs, "BUILD-03", helpers.standard_package())
        self.claim = self.upload("supplier_claim", CLAIM, "ev-claim-1")
        self.transport = self.upload("transport_record", TRANSPORT, "ev-trans-1")
        status, body = self.service.open_review("LOT-A", HOLD_REASON, [self.claim, self.transport], REVIEWER,
                                                "review-open-1")
        assert status == 201, body
        self.review_id = body["review_id"]
        assert self.service.import_package("BUILD-03", "operator (local:op)", "import-1")[0] == 200
        self.service.drain_outbox()
        tele = telemetry.Telemetry(sink) if sink else None
        app = create_app(Settings(auth_mode="local", worker=False, **settings), self.service, telemetry=tele,
                         ledger=ledger)
        self.client = TestClient(app)

    def upload(self, kind, value, command_id):
        status, body = self.service.upload_evidence(kind, "LOT-A", f"{kind}.json", "application/json",
                                                    json.dumps(value).encode(), REVIEWER, command_id)
        assert status == 201, body
        return body["evidence_id"]

    def setUp(self):
        self.build()

    def tearDown(self):
        self.client.close()


class RoleTests(Screens):
    def test_staff_screens_refuse_anonymous_and_role_less_users_with_an_html_page(self):
        for path in ("/exec", "/trail/BAT-103", f"/holds/{self.review_id}"):
            anonymous = self.client.get(path)
            self.assertEqual(anonymous.status_code, 401, path)
            self.assertIn("Please sign in", anonymous.text)
            self.assertNotIn("/.auth/login", anonymous.text)  # local mode has no Easy Auth login
            roleless = self.client.get(path, headers=NOBODY)
            self.assertEqual(roleless.status_code, 403, path)
            self.assertIn(BANNER, roleless.text)
            self.assertNotIn("BAT-101", roleless.text)
            for staff in (OP, REV):
                self.assertEqual(self.client.get(path, headers=staff).status_code, 200, path)

    def test_only_a_reviewer_can_resolve_a_hold(self):
        url = f"/holds/{self.review_id}/resolve"
        data = {"reason": "Corrected claim matches transport", "evidence_id": self.claim, "command_id": "cmd-role-0001",
                "expected_row_version": "1"}
        self.assertEqual(self.client.post(url, data=data).status_code, 401)
        self.assertEqual(self.client.post(url, data=data, headers=NOBODY).status_code, 403)
        self.assertEqual(self.client.post(url, data=data, headers=OP).status_code, 403)
        self.assertEqual(self.service.board()["text"], "1 of 3 passports committed")

    def test_ledger_verify_is_staff_only_json(self):
        self.assertEqual(self.client.get("/ledger/verify").status_code, 401)
        self.assertEqual(self.client.get("/ledger/verify", headers=NOBODY).status_code, 403)
        result = self.client.get("/ledger/verify?battery=BAT-103", headers=OP).json()
        self.assertEqual((result["status"], result["reason_code"], result["version"]),
                         ("unavailable", "memory_store", 1))
        self.assertEqual(self.client.get("/ledger/verify?battery=NOPE-1", headers=OP).status_code, 404)


class PublicPassportTests(Screens):
    def test_published_battery_shows_public_fields_and_an_inline_qr_code(self):
        page = self.client.get("/p/BAT-103")
        self.assertEqual(page.status_code, 200)
        for text in (BANNER, "Published", "Passport version", "<svg", 'class="qr"',
                     "http://testserver/p/BAT-103", "Passport link"):
            self.assertIn(text, page.text)
        self.assertNotIn("<script", page.text)
        self.assertIn("Contoso Battery", page.text)
        self.assertNotIn("not a legal passport", page.text)
        self.assertNotIn("Decision trail", page.text)  # the staff link stays hidden from anonymous visitors
        card = self.client.get("/p/BAT-103/card")
        self.assertEqual(card.status_code, 200)
        self.assertIn("<svg", card.text)
        self.assertIn("viewBox", card.text)  # scalable on a phone and in print, not clipped at a fixed size

    def test_held_battery_shows_not_yet_issued_without_restricted_data_or_reason(self):
        page = self.client.get("/p/BAT-101")
        self.assertEqual(page.status_code, 200)
        self.assertIn(exec_views.PUBLIC_NOT_ISSUED, page.text)
        for hidden in (HOLD_REASON, "LOT-A", "Mine North", "Passport version", "Ledger"):
            self.assertNotIn(hidden, page.text)

    def test_unknown_battery_is_404(self):
        self.assertEqual(self.client.get("/p/BAT-999").status_code, 404)

    def test_configured_public_address_is_printed_in_the_qr_link(self):
        self.client.close()
        self.build(public_base_url="https://passport.example")
        self.assertIn("https://passport.example/p/BAT-103", self.client.get("/p/BAT-103").text)


class ExecTests(Screens):
    def test_passport_readiness_counts_held_batteries_and_admits_what_was_not_measured(self):
        page = self.client.get("/exec", headers=OP).text
        for text in (BANNER, "Production series V3", "1 of 3 published", "Coverage 33%", "(held batteries count)",
                     "Review the hold on LOT-A", f"/holds/{self.review_id}", "tokens not measured",
                     exec_views.CARBON_LABEL, "/p/BAT-101", "/trail/BAT-103", "Complete (2 of 2)"):
            self.assertIn(text, page)
        self.assertIn("No model prices are configured", page)
        self.assertNotIn("USD 0", page)

    def test_live_defaults_to_five_seconds_and_can_be_paused_across_navigation(self):
        page = self.client.get("/exec", headers=REV).text
        self.assertIn('http-equiv="refresh" content="5"', page)
        self.assertIn("Live: refreshes every 5 seconds", page)
        self.assertIn("&amp;live=0", page)
        self.client.get("/exec?live=0", headers=REV)
        self.assertNotIn('http-equiv="refresh"', self.client.get("/exec", headers=REV).text)
        self.assertIn('http-equiv="refresh" content="5"', self.client.get("/exec?live=1", headers=REV).text)
        self.assertNotIn('http-equiv="refresh"', self.client.get(f"/holds/{self.review_id}", headers=REV).text)

    def test_unknown_series_never_silently_displays_a_different_series(self):
        for remembered in (None, "SERIES-REMOVED-BY-CLEAN"):
            with self.subTest(remembered=remembered):
                if remembered:
                    self.client.cookies.set(experience.SERIES_COOKIE, remembered, domain="testserver.local")
                page = self.client.get("/exec?series=NOT-A-SERIES", headers=REV)
                self.assertEqual(page.status_code, 404)
                self.assertIn("Selection unavailable", page.text)
                self.assertNotIn("BAT-101", page.text)
                self.assertNotIn("Your saved production series is no longer available", page.text)

    def test_a_remembered_series_that_no_longer_exists_is_forgotten(self):
        for tour in (None, "reviewer|hold|SERIES-REMOVED-BY-CLEAN"):
            with self.subTest(tour=tour):
                self.client.cookies.set(experience.SERIES_COOKIE, "SERIES-REMOVED-BY-CLEAN",
                                        domain="testserver.local")
                if tour:
                    self.client.cookies.set(experience.JOURNEY_COOKIE, tour, domain="testserver.local")
                page = self.client.get("/exec", headers=REV)
                self.assertEqual(page.status_code, 200)
                self.assertIn("BAT-101", page.text)
                self.assertNotIn("Selection unavailable", page.text)
                self.assertIn("Your saved production series is no longer available. Showing Production series V3.",
                              page.text)
                self.assertEqual(self.client.cookies.get(experience.SERIES_COOKIE), "SERIES-V3")
                self.assertIsNone(self.client.cookies.get(experience.JOURNEY_COOKIE))
                self.assertNotIn('class="journey-guide', page.text)
                self.assertIn('http-equiv="refresh" content="5"', page.text)
                self.assertNotIn("Your saved production series is no longer available",
                                 self.client.get("/exec", headers=REV).text)

    def test_waiting_status_keeps_only_the_dedicated_live_control(self):
        service, blobs = helpers.new_service()
        package = helpers.make_package("BUILD-03", 1, [helpers.battery("BAT-101", "LOT-A", readiness="blocked")])
        helpers.put_package(blobs, "BUILD-03", package)
        self.assertEqual(service.import_package("BUILD-03", helpers.OPERATOR, "pending-ui-1")[0], 200)
        with TestClient(create_app(Settings(auth_mode="local", worker=False), service)) as client:
            for live in (False, True):
                with self.subTest(live=live):
                    response = client.get(f"/exec?live={int(live)}", headers=OP)
                    self.assertEqual(response.status_code, 200)
                    page = response.text
                    card = re.search(r'<div class="next">(.*?)</div>', page, re.S).group(1)
                    self.assertIn("No human decision is needed right now.", card)
                    self.assertNotIn("<a ", card)
                    self.assertNotIn("<button", card)
                    self.assertNotIn("Watch the agents work", page)
                    self.assertEqual(page.count("&amp;live=1"), 0 if live else 1)
                    self.assertIn("Stop refreshing" if live else "Live mode", page)
                    self.assertEqual('http-equiv="refresh"' in page, live)

    def test_status_says_the_agents_are_still_working_when_no_person_is_needed(self):
        service, blobs = helpers.new_service()
        service.agent_mode = True
        helpers.put_package(blobs, "BUILD-03", helpers.make_package("BUILD-03", 1, [helpers.battery("BAT-101",
                                                                                                    "LOT-A")]))
        self.assertEqual(service.import_package("BUILD-03", helpers.OPERATOR, "pending-ui-2")[0], 200)
        with TestClient(create_app(Settings(auth_mode="local", worker=False), service)) as client:
            page = client.get("/exec", headers=OP).text
        card = re.search(r'<div class="next">(.*?)</div>', page, re.S).group(1)
        self.assertIn("No human decision is needed right now. The agents are still working on 1 battery", card)
        self.assertNotIn("<a ", card)


class HoldTests(Screens):
    def test_reviewer_sees_claim_and_transport_side_by_side_with_the_difference(self):
        page = self.client.get(f"/holds/{self.review_id}", headers=REV).text
        for text in ("Supplier claim", "Transport record", "Mine North", "Mine South", "Differs", HOLD_REASON,
                     "No new document is on file"):
            self.assertIn(text, page)
        self.assertNotIn(f'action="/holds/{self.review_id}/resolve"', page)

    def test_operator_sees_document_names_but_not_their_content_or_the_form(self):
        self.upload("corrected_claim", CORRECTED, "ev-fix-1")
        page = self.client.get(f"/holds/{self.review_id}", headers=OP).text
        self.assertIn("supplier_claim.json", page)
        self.assertIn("Only a Passport.Reviewer", page)
        for hidden in ("Mine North", "Mine South", f'action="/holds/{self.review_id}/resolve"', "command_id"):
            self.assertNotIn(hidden, page)

    def test_reason_is_validated_and_the_decision_reruns_the_checks_without_the_person_publishing(self):
        correction = self.upload("corrected_claim", CORRECTED, "ev-fix-1")
        page = self.client.get(f"/holds/{self.review_id}", headers=REV).text
        self.assertIn("Matches transport", page)
        first = form_value(page, "command_id")
        data = {"evidence_id": correction, "command_id": first,
                "expected_row_version": form_value(page, "expected_row_version")}
        short = self.client.post(f"/holds/{self.review_id}/resolve", headers=REV, data={**data, "reason": "no"})
        self.assertEqual(short.status_code, 422)
        self.assertIn("Write a reason of 5 to 400 characters.", short.text)
        self.assertNotEqual(form_value(short.text, "command_id"), first)
        self.assertEqual(self.service.board()["text"], "1 of 3 passports committed")
        long = self.client.post(f"/holds/{self.review_id}/resolve", headers=REV,
                                data={**data, "command_id": "cmd-long-0001", "reason": "x" * 401})
        self.assertEqual(long.status_code, 422)
        self.assertIn("Write a reason of 5 to 400 characters.", long.text)
        done = self.client.post(f"/holds/{self.review_id}/resolve", headers=REV, follow_redirects=False,
                                data={**data, "command_id": "cmd-ok-00001",
                                      "reason": "Corrected claim matches transport"})
        self.assertEqual(done.status_code, 303)
        self.assertEqual(done.headers["location"], f"/holds/{self.review_id}?done=1")
        after = self.client.get(done.headers["location"], headers=REV).text
        self.assertIn("Decision recorded", after)
        self.assertIn("you did not publish anything", after)
        self.assertIn('href="/exec">Back to passport readiness', after)
        readiness = self.client.get("/exec", headers=OP).text
        self.assertIn("3 of 3 published", readiness)
        self.assertIn("Open a public passport", readiness)

    def test_stale_form_is_refused(self):
        correction = self.upload("corrected_claim", CORRECTED, "ev-fix-1")
        response = self.client.post(f"/holds/{self.review_id}/resolve", headers=REV, data={
            "evidence_id": correction, "command_id": "cmd-old-0001", "expected_row_version": "",
            "reason": "Corrected claim matches transport"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("The form expired", response.text)

    def test_unknown_hold_is_404(self):
        self.assertEqual(self.client.get("/holds/RV-NOPE", headers=REV).status_code, 404)


class ExperienceTests(Screens):
    def setUp(self):
        super().setUp()
        self.headers = {"x-local-user": "same-person", "x-local-roles": ",".join(sorted(experience.HUMAN_ROLES))}

    def choose(self, choice, target="/exec", headers=None):
        return self.client.post("/personas", headers=headers or self.headers,
                                data={"persona": choice, "return_to": target}, follow_redirects=False)

    def test_role_bar_keeps_noscript_switch_and_one_journey_entry(self):
        for choice, target in (("all", "/exec"), ("reviewer", "/exec"), ("public", "/p/BAT-103")):
            with self.subTest(choice=choice):
                self.assertEqual(self.choose(choice, target).status_code, 303)
                response = self.client.get(target, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                page = response.text
                picker = re.search(r'<form[^>]*class="persona-picker">(.*?)</form>', page, re.S).group(1)
                fallback = re.search(r"<noscript>(.*?)</noscript>", picker, re.S)
                self.assertIsNotNone(fallback)
                self.assertIn('type="submit">Switch</button>', fallback.group(1))
                self.assertNotIn("<button", picker.replace(fallback.group(0), ""))
                self.assertNotIn("Start guided journey", page)
                self.assertEqual(page.count('href="/journey"'), 1)
                self.assertEqual('class="journey-entry"' in page, choice == "public")

    def test_switch_narrows_server_permissions_and_preserves_audit_identity(self):
        correction = self.upload("corrected_claim", CORRECTED, "ev-persona-fix")
        self.assertEqual(self.choose("compliance").headers["location"], "/cases")
        me = self.client.get("/me", headers=self.headers).json()
        self.assertEqual((me["name"], me["user_id"], me["roles"]),
                         ("same-person", "local:same-person", ["Passport.Compliance"]))
        self.assertEqual(self.client.get("/exec", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post(f"/holds/{self.review_id}/resolve",
                                         headers=self.headers, data={}).status_code, 403)
        self.choose("reviewer")
        page = self.client.get(f"/holds/{self.review_id}", headers=self.headers).text
        done = self.client.post(f"/holds/{self.review_id}/resolve", headers=self.headers, data={
            "evidence_id": correction, "reason": "The corrected evidence now matches the transport record.",
            "command_id": form_value(page, "command_id"),
            "expected_row_version": form_value(page, "expected_row_version")}, follow_redirects=False)
        self.assertEqual(done.status_code, 303)
        with self.service.store.read() as tx:
            review = tx.get("Reviews", self.review_id)
        self.assertIn("same-person", json.dumps(review))
        self.assertEqual(self.client.post("/packages/import", headers=self.headers, json={}).status_code, 403)

    def test_forged_and_revoked_choices_fail_closed_and_available_choices_are_filtered(self):
        page = self.client.get("/exec", headers=REV).text
        self.assertIn('value="reviewer"', page)
        self.assertNotIn('value="admin"', page)
        self.assertEqual(self.choose("admin", headers=REV).status_code, 403)
        self.client.cookies.set(experience.PERSONA_COOKIE, "admin")
        self.assertEqual(self.client.get("/me", headers=REV).json()["roles"], [])
        self.assertEqual(self.client.get("/settings", headers=REV).status_code, 403)
        self.client.cookies.clear()
        self.choose("compliance")
        self.assertEqual(self.client.get("/me", headers=REV).json()["roles"], [])
        self.assertIn("no longer available", self.client.get("/", headers=REV).text)
        self.choose("all", headers=REV)
        self.assertEqual(self.client.get("/me", headers=REV).json()["roles"], ["Passport.Reviewer"])

    def test_public_preview_and_bearer_calls_keep_their_distinct_permissions(self):
        self.choose("public", "/p/BAT-103")
        view = self.client.get("/passports/BAT-103/view", headers=self.headers).json()
        self.assertEqual(view["field_groups"], ["identity", "footprint"])
        self.assertEqual(self.client.get("/me", headers=self.headers).json()["user_id"], "local:same-person")
        api = self.client.get("/me", headers={**self.headers, "authorization": "Bearer token"})
        self.assertEqual(set(api.json()["roles"]), experience.HUMAN_ROLES)
        self.choose("all")
        self.assertEqual(set(self.client.get("/me", headers=self.headers).json()["roles"]), experience.HUMAN_ROLES)

    def test_switching_requires_authentication_same_origin_and_a_safe_landing_page(self):
        self.assertEqual(self.client.post("/personas", data={"persona": "reviewer"}).status_code, 401)
        for endpoint, data in (("/personas", {"persona": "reviewer"}),
                               ("/journey", {"step": "outcome", "series": "SERIES-V3"})):
            self.assertEqual(self.client.post(endpoint, headers={**self.headers, "origin": "https://foreign.example"},
                                              data=data).status_code, 403)
        for target in ("https://foreign.example", "//foreign.example", "//[invalid", "/\\foreign.example",
                       "/.auth/logout",
                       "/%2f%2fforeign.example", "/exec\nLocation: https://foreign.example"):
            self.assertEqual(self.choose("oem", target).headers["location"], "/", target)

    def test_journey_navigation_changes_no_business_records_and_survives_forms_and_refresh(self):
        self.choose("reviewer")
        with self.service.store.read() as tx:
            before = tx.tables
        for stop in ("outcome", "evidence", "hold", "evidence", "hold"):
            response = self.client.post("/journey", headers=self.headers,
                                        data={"persona": "reviewer", "step": stop, "series": "SERIES-V3"})
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="journey-guide no-print"', response.text)
        with self.service.store.read() as tx:
            self.assertEqual(tx.tables, before)
        invalid = self.client.post(f"/holds/{self.review_id}/resolve", headers=self.headers,
                                  data={"reason": "too short"})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("Resolve the lithium discrepancy", invalid.text)
        self.assertNotIn('http-equiv="refresh"', self.client.get("/exec?live=1", headers=self.headers).text)
        self.client.post("/journey", headers=self.headers, data={"step": "exit"})
        self.assertNotIn('class="journey-guide no-print"', self.client.get("/exec", headers=self.headers).text)

    def test_unavailable_stops_are_explicit_and_never_resolve_another_series(self):
        self.choose("reviewer")
        page = self.client.post("/journey", headers=self.headers,
                                data={"persona": "reviewer", "step": "hold", "series": "MISSING"})
        self.assertIn("No package for this series", page.text)
        self.assertEqual(page.url.path, "/journey")
        target, missing = experience.resolve(self.service, experience.BY_KEY["hold"], "MISSING")
        self.assertIsNone(target)
        self.assertTrue(missing)
        self.choose("compliance")
        absent = self.client.post("/journey", headers=self.headers,
                                  data={"persona": "compliance", "step": "compliance", "series": "SERIES-V3"})
        self.assertIn("No compliance case exists", absent.text)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/journey", headers=REV,
                                         data={"step": "policy", "series": "SERIES-V3"}).status_code, 403)
        self.assertEqual(self.client.post("/journey", headers=self.headers,
                                         data={"persona": "reviewer", "step": "unknown",
                                               "series": "SERIES-V3"}).status_code, 403)
        self.choose("reviewer")
        self.assertEqual(self.client.post("/journey", headers=self.headers,
                                         data={"persona": "reviewer", "step": "unknown",
                                               "series": "SERIES-V3"}).status_code, 422)

    def test_tour_options_follow_selected_or_single_role_and_exclude_admin_and_authority(self):
        self.assertIn("Choose one role to begin", self.client.get("/journey", headers=self.headers).text)
        self.choose("compliance")
        page = self.client.get("/journey", headers=self.headers).text
        self.assertIn("4 steps for Compliance Officer", page)
        self.assertIn('value="case-evidence"', page)
        for excluded in ("hold", "policy", "oem", "outcome"):
            self.assertNotIn(f'value="{excluded}">', page.split('id="journey-stop"')[1])
        self.client.cookies.clear()
        page = self.client.get("/journey", headers=COMP).text
        self.assertIn("4 steps for Compliance Officer", page)
        self.assertIn('<option value="SERIES-V3"', page)
        for profile in ("authority", "admin"):
            self.choose(profile)
            page = self.client.get("/journey", headers=self.headers).text
            self.assertIn("No tour for this persona", page)
            self.assertNotIn('class="journey-start"', page)
            self.assertNotIn("Start guided journey", page)
            self.client.cookies.clear()
            single = {**self.headers, "x-local-roles": experience.PROFILES[profile][1]}
            page = self.client.get("/journey", headers=single).text
            self.assertIn("No tour for this persona", page)
            self.assertNotIn("Start guided journey", page)

    def test_navigation_cannot_switch_personas_even_when_the_account_holds_both_roles(self):
        self.choose("reviewer")
        data = {"persona": "reviewer", "step": "hold", "series": "SERIES-V3"}
        self.assertEqual(self.client.post("/journey", headers=self.headers, data=data).status_code, 200)
        for forged in ({**data, "step": "compliance"}, {**data, "persona": "compliance", "step": "compliance"}):
            self.assertEqual(self.client.post("/journey", headers=self.headers, data=forged).status_code, 403)
        self.assertEqual(self.client.get("/me", headers=self.headers).json()["roles"], ["Passport.Reviewer"])
        self.choose("compliance")
        state = self.client.cookies.get(experience.JOURNEY_COOKIE)
        self.assertEqual(state, "compliance|case-context|SERIES-V3")
        self.assertEqual(self.client.post("/journey", headers=self.headers, data=data).status_code, 403)
        page = self.client.get("/journey", headers=self.headers).text
        self.assertIn("Stop 1 of 4", page)
        self.choose("admin")
        self.assertIsNone(self.client.cookies.get(experience.JOURNEY_COOKIE))

    def test_stale_or_revoked_tours_are_not_rendered_and_single_role_needs_no_switch(self):
        self.client.cookies.set(experience.JOURNEY_COOKIE, "hold|SERIES-V3")  # retired cross-role format
        self.assertNotIn('class="journey-guide', self.client.get("/journey", headers=REV).text)
        page = self.client.post("/journey", headers=REV,
                                data={"persona": "reviewer", "step": "hold", "series": "SERIES-V3"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Reviewer tour", page.text)
        self.assertIsNone(self.client.cookies.get(experience.PERSONA_COOKIE))
        self.assertNotIn('class="journey-guide', self.client.get("/journey", headers=COMP).text)
        self.assertEqual(self.client.post("/journey", headers=COMP,
                                         data={"persona": "reviewer", "step": "hold",
                                               "series": "SERIES-V3"}).status_code, 403)

    def test_easyauth_cookies_are_secure_and_private_pages_are_not_cached(self):
        app = create_app(Settings(auth_mode="easyauth", worker=False), self.service)
        claims = [{"typ": "roles", "val": role} for role in experience.HUMAN_ROLES]
        claims += [{"typ": "oid", "val": "signed-in-user"}, {"typ": "name", "val": "same-person"}]
        headers = {"x-ms-client-principal": base64.b64encode(json.dumps({"claims": claims}).encode()).decode()}
        with TestClient(app, base_url="https://testserver") as client:
            response = client.post("/personas", headers=headers,
                                   data={"persona": "reviewer", "return_to": "/exec"}, follow_redirects=False)
            for flag in ("Secure", "HttpOnly", "SameSite=strict"):
                self.assertIn(flag, response.headers["set-cookie"])
            self.assertEqual(client.get("/me", headers=headers).json()["roles"], ["Passport.Reviewer"])
            page = client.get("/exec", headers=headers)
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertIn("script-src 'self'", page.headers["content-security-policy"])
            script = client.get("/static/experience.js")
            self.assertEqual(script.status_code, 200)
            self.assertIn("requestSubmit", script.text)


class TrailTests(Screens):
    def test_trail_shows_the_rule_step_and_does_not_claim_a_ledger_check_locally(self):
        page = self.client.get("/trail/BAT-103", headers=OP).text
        for text in (BANNER, "Decision timeline", "Application rule", "Deterministic rule (no AI)",
                     exec_views.LEDGER_TITLES["unavailable"], "Snapshot integrity: the stored passport copy matches",
                     "0 agent runs", "tokens not measured"):
            self.assertIn(text, page)
        self.assertNotIn("&amp;middot;", page)

    def test_trail_has_a_key_and_collapsible_steps_with_an_expand_all_script(self):
        page = self.client.get("/trail/BAT-101", headers=REV).text
        for text in ('aria-label="Key to the step icons"', "AI agent", "Person", "Application rule",
                     "Deterministic, no AI", 'class="swatch k-ai"', 'class="swatch k-person"', 'class="swatch k-app"',
                     '<details class="card step-card">', "Show details",
                     'data-expand-all aria-controls="steps-list" hidden', 'src="/static/trail.js?v=1"'):
            self.assertIn(text, page)
        self.assertNotIn("Refused AI proposal", page)  # the key lists only what this trail contains
        script = self.client.get("/static/trail.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("beforeprint", script.text)
        self.assertNotIn("trail.js", self.client.get("/exec", headers=OP).text)

    def test_scripted_local_runs_get_their_own_key_instead_of_the_ai_agent_key(self):
        agents = AgentService(self.service)
        task = agents.prepare("carbon", "BAT-103", None, "operator (local:op)", "carbon-script-0001")
        for section in SECTIONS["carbon"]:
            agents.section(task["task_id"], "carbon", section)
        status, body = agents.finish(task, {"action": "calculate", "reason": "Scripted calculation request.",
                                            "evidence_ids": []}, {"hosting": "local_scripted_double"})
        self.assertEqual(status, 200, body)
        page = self.client.get("/trail/BAT-103", headers=OP).text
        self.assertIn("<strong>Scripted agent (not AI)</strong>", page)
        self.assertNotIn("<strong>AI agent</strong>", page)

    def test_trail_identifies_shared_lots_sources_and_creation_order_not_completion_order(self):
        agents = AgentService(self.service)
        tasks = []
        for lot, created, actor in (
                ("LOT-NI", "2026-11-11T10:00:01.100+01:00", "workflow:application-event"),
                ("LOT-CO", "2026-11-11T09:00:01.900Z", "olivia (local:op)")):
            self.service.clock = lambda created=created: created
            status, uploaded = self.service.upload_evidence(
                "supplier_claim", lot, f"{lot}-claim.json", "application/json", b'{"origin_site":"Site A"}',
                REVIEWER, f"source-{lot}")
            self.assertEqual(status, 201)
            task = agents.prepare("supplier", lot, None, actor, f"supplier-{lot}")
            for section in SECTIONS["supplier"]:
                agents.section(task["task_id"], "supplier", section)
            tasks.append((task, uploaded["evidence_id"]))
        for (task, evidence), finished in zip(tasks, ("2026-11-11T09:00:08.100Z", "2026-11-11T09:00:05.100Z"),
                                             strict=True):
            self.service.clock = lambda finished=finished: finished
            status, body = agents.finish(task, {
                "action": "no_issue", "reason": "Claim has no conflicting evidence.", "evidence_ids": [evidence]},
                {"hosting": "hosted", "model": "test-model"})
            self.assertEqual(status, 200, body)
        self.service.clock = lambda: "2026-11-11T09:00:09.100Z"
        agents.prepare("carbon", "BAT-101", None, "workflow:application-event", "carbon-pending")
        self.service.upload_evidence("other", "LOT-NI", "newer-evidence.json", "application/json", b'{"new":true}',
                                     REVIEWER, "later-evidence")

        view = exec_views.trail_view(self.service, "BAT-101")
        runs = [s for s in view["path"] if s["kind"] == "agent"]
        self.assertEqual([r["subject"] for r in runs], ["LOT-NI", "LOT-CO", "BAT-101"])
        self.assertEqual(runs[0]["when"], "11 Nov 2026, 09:00:01.100 UTC")
        self.assertEqual(runs[0]["completed_when"], "11 Nov 2026, 09:00:08.100 UTC")
        self.assertEqual(runs[1]["completed_when"], "11 Nov 2026, 09:00:05.100 UTC")
        self.assertEqual(runs[2]["completed_when"], "No result yet")
        self.assertEqual([s["name"] for s in runs[0]["sources"]], ["LOT-NI-claim.json"])
        shared = exec_views.trail_view(self.service, "BAT-102")
        self.assertEqual([s["task_id"] for s in shared["path"] if s["kind"] == "agent"],
                         [task["task_id"] for task, _ in tasks])

        page = self.client.get("/trail/BAT-101", headers=OP).text
        for text in ("Supplier - LOT-NI (no issue)", "Supplier - LOT-CO (no issue)",
                     "Supplier agent \u00b7 LOT-NI", "Carbon agent \u00b7 BAT-101",
                     "Source: Automatic application workflow", "Source: Operator/API request by olivia",
                     "Task created 11 Nov 2026, 09:00:01.100 UTC",
                     "Result recorded: 11 Nov 2026, 09:00:08.100 UTC", "Result recorded: No result yet",
                     'href="#task-supplier-LOT-NI"', 'href="/lots/LOT-NI"', "LOT-NI-claim.json",
                     "Evidence supplied to this task", "same task appears", "not a chain of agent calls",
                     "different assessments, not retries", "Task ID", "Input hash"):
            self.assertIn(text, page)
        self.assertNotIn("newer-evidence.json", page)
        self.assertNotIn("Who acted, in order.", page)
        self.assertLess(page.index('id="task-supplier-LOT-NI"'), page.index('id="task-supplier-LOT-CO"'))

    def test_held_battery_trail_shows_the_opened_hold_and_nothing_to_verify(self):
        page = self.client.get("/trail/BAT-101", headers=REV).text
        self.assertIn("Opened hold", page)
        self.assertIn("nothing to verify", page)
        self.assertEqual(self.client.get("/trail/BAT-999", headers=REV).status_code, 404)

    def test_import_footprint_is_not_presented_as_carbon_agent_or_verifier_evidence(self):
        view = exec_views.trail_view(self.service, "BAT-101")
        calculation = next(step for step in view["path"] if step["kind"] == "calculation")
        self.assertIsNone(view["version"])
        self.assertFalse(any(step["kind"] == "agent" for step in view["path"]))
        self.assertEqual(calculation["tone"], "info")
        page = self.client.get("/trail/BAT-101", headers=OP).text
        for text in ("Import-time footprint", "Footprint calculation at import", "Initial footprint (illustrative)",
                     "without invoking an agent", "no Carbon-agent or independent-verifier evidence",
                     "Issuance requires both before publication"):
            self.assertIn(text, page)
        with self.service.store.read() as tx:
            package = tx.get("Packages", "BUILD-03")
            row = tx.get("PackageBatteries", "BUILD-03", "BAT-101")
        steps = exec_views.package_steps(package, {**row, "footprint_per_kwh": None}, None, 9, 9)
        unavailable = next(step for step in steps if step["kind"] == "calculation")
        self.assertEqual(unavailable["tone"], "warn")
        self.assertIn("Import-time footprint unavailable", unavailable["text"])
        self.assertNotIn("Initial footprint", unavailable["text"])

    def test_verified_and_failed_ledger_results_are_shown_and_only_staff_trigger_a_check(self):
        self.client.close()
        ledger = FakeLedger()
        self.build(ledger=ledger)
        self.assertIn("Ledger check not shown", self.client.get("/p/BAT-103").text)
        self.assertEqual(ledger.calls, [])  # an anonymous page never runs the check
        self.assertIn("Ledger verified at 11 Nov 2026, 09:30 UTC", self.client.get("/trail/BAT-103", headers=OP).text)
        self.assertEqual(ledger.calls, [("BAT-103", 1)])
        self.assertIn("Ledger verified at 11 Nov 2026, 09:30 UTC", self.client.get("/p/BAT-103").text)
        ledger.status = "failed"
        self.assertIn("Ledger verification FAILED at", self.client.get("/trail/BAT-103", headers=OP).text)


class NavigationTests(Screens):
    SIDEBAR = 'class="side"'

    def test_staff_sidebar_lists_the_batch_its_batteries_and_open_holds(self):
        page = self.client.get("/exec", headers=OP).text
        hold = f'href="/holds/{self.review_id}"'
        for text in (self.SIDEBAR, 'href="/exec" aria-current="page"', "1/3", "Production series V3</p>",
                     'href="/p/BAT-101"', 'href="/trail/BAT-103"', "Published", "On hold", hold, "1 open"):
            self.assertIn(text, page)
        self.assertNotIn(f'{hold} aria-current', page)

    def test_the_current_page_is_marked_in_the_sidebar(self):
        trail = self.client.get("/trail/BAT-103", headers=REV).text
        self.assertIn('href="/trail/BAT-103" aria-current="page"', trail)
        self.assertNotIn('href="/exec" aria-current', trail)
        self.assertIn('href="/p/BAT-103" aria-current="page"', self.client.get("/p/BAT-103", headers=OP).text)
        hold = self.client.get(f"/holds/{self.review_id}", headers=REV).text
        self.assertIn(f'href="/holds/{self.review_id}" aria-current="page"', hold)

    def test_anonymous_role_less_and_printed_pages_have_no_sidebar(self):
        for page in (self.client.get("/p/BAT-101"), self.client.get("/p/BAT-103"),
                     self.client.get("/exec", headers=NOBODY), self.client.get("/exec"),
                     self.client.get("/p/BAT-103/card", headers=OP)):
            self.assertNotIn(self.SIDEBAR, page.text)
            self.assertNotIn("/trail/", page.text)


class SeriesScopeTests(Screens):
    HEADERS = {**REV, "accept": "text/html"}

    def setUp(self):
        super().setUp()
        battery = helpers.battery("BAT-201", "LOT-OTHER")
        battery["series_id"] = "SERIES-OTHER"
        # Cobalt is deliberately shared; other lots belong to only one series.
        battery["parts"] = [(material, lot if material in ("lithium", "cobalt") else lot + "-OTHER", mass)
                            for material, lot, mass in battery["parts"]]
        files = helpers.make_package("BUILD-04", 3, [battery])
        manifest = json.loads(files["manifest.json"])
        manifest["series_id"] = "SERIES-OTHER"
        files["manifest.json"] = helpers.dumps(manifest)
        helpers.put_package(self.blobs, "BUILD-04", files)
        self.assertEqual(self.service.import_package("BUILD-04", helpers.OPERATOR, "import-other-1")[0], 200)
        self.other_claim = self.document("LOT-OTHER", "supplier_claim", "other-claim-1")
        status, review = self.service.open_review("LOT-OTHER", "Other series sourcing concern",
                                                  [self.other_claim], REVIEWER, "other-review-1")
        self.assertEqual(status, 201)
        self.other_review = review["review_id"]
        self.cases = {}
        for lot in ("LOT-B", "LOT-OTHER"):
            evidence = self.document(lot, "adverse_report", f"adverse-{lot}")
            status, case = self.service.open_case(lot, "Read this sourcing report", [evidence],
                                                  "compliance (local:co)", f"case-{lot}")
            self.assertEqual(status, 201)
            self.cases[lot] = case["case_id"]
        with self.service.store.transaction() as tx:
            for number, battery_id in enumerate(("BAT-103", "BAT-201"), 1):
                tx.insert("SecondLifeRequests", {
                    "request_id": f"SL-{number:04d}", "battery_id": battery_id,
                    "successor_id": f"{battery_id}-SL1", "requested_by": "Second-life operator",
                    "message_id": f"MSG-{number}", "safety_assessment_ref": "SAFETY-1",
                    "intended_use": "Stationary storage", "soh_pct": 82.0, "illustrative_value_eur": 1500.0,
                    "value_label": "Illustrative value", "status": "pending", "decision_reason": None,
                    "decided_by": None, "decided_utc": None, "requested_utc": self.service.clock(), "row_version": 1})

    def document(self, lot, kind, command):
        status, body = self.service.upload_evidence(
            kind, lot, f"{command}.json", "application/json", b'{"note":"synthetic evidence"}', REVIEWER, command)
        self.assertEqual(status, 201, body)
        return body["evidence_id"]

    def select(self, series):
        response = self.client.get(f"/exec?series={series}", headers=self.HEADERS)
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_selected_series_scopes_navigation_lists_console_and_details(self):
        page = self.select("SERIES-V3")
        self.assertLess(page.index('id="persona-select"'), page.index('id="series-select"'))
        self.assertLess(page.index('id="series-select"'), page.index('<main id="main-content">'))
        for path in ("/exec", "/", "/cases", "/second-life", f"/holds/{self.review_id}", "/lots/LOT-B",
                     f"/cases/{self.cases['LOT-B']}", "/p/BAT-103", "/trail/BAT-103"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.HEADERS)
                self.assertEqual(response.status_code, 200)
                for foreign in ("BAT-201", "LOT-OTHER", self.other_review, self.cases["LOT-OTHER"], "SL-0002",
                                self.other_claim, "BUILD-04"):
                    self.assertNotIn(foreign, response.text)
        cases = self.client.get("/cases", headers=self.HEADERS).text
        self.assertIn(self.cases["LOT-B"], cases)
        self.assertIn("SL-0001", self.client.get("/second-life", headers=self.HEADERS).text)
        for headers in (OP, COMP, AUTH, {**REV, "x-local-roles": "Passport.OEM"}):
            page = self.client.get("/", headers=headers).text
            self.assertIn("BAT-103", page)
            self.assertNotIn("BAT-201", page)
        # Browser selection must not filter the established token/JSON API contracts.
        data = self.client.get("/second-life", headers=REV).json()
        self.assertEqual(len(data["requests"]), 2)

    def test_console_logs_filter_before_limiting_and_other_series_uses_its_own_lots(self):
        with self.service.store.transaction() as tx:
            for number, subject in enumerate(["LOT-B"] + ["LOT-OTHER"] * 30):
                tx.insert("ConnectorMessages", {
                    "receipt_id": f"RECEIPT-{number:03d}", "message_id": f"CLAIM-{number:03d}",
                    "sender": "Supplier", "subject": subject, "message_type": "supplier_lot_claim",
                    "result": "verified", "reason_code": "ok", "received_utc": self.service.clock()})
        self.select("SERIES-V3")
        page = self.client.get("/", headers=OP).text
        self.assertIn("CLAIM-000", page)
        self.assertNotIn("CLAIM-030", page)
        self.assertNotIn("BAT-201", page)
        self.select("SERIES-OTHER")
        page = self.client.get("/", headers=self.HEADERS).text
        self.assertIn('<option value="LOT-OTHER">', page)
        self.assertNotIn('<option value="LOT-A">', page)
        self.assertNotIn('name="lot_id" value="LOT-A"', page)

    def test_all_series_combines_records_and_stays_selected_on_detail_pages(self):
        page = self.select("all")
        self.assertIn('<option value="all" selected>All production series</option>', page)
        self.assertIn("2 of 4 published", page)
        for path in ("/exec", "/", "/cases", "/second-life", f"/holds/{self.review_id}"):
            response = self.client.get(path, headers=self.HEADERS)
            self.assertEqual(response.status_code, 200)
            for included in ("BAT-101", "BAT-201", self.review_id, self.other_review,
                             self.cases["LOT-B"], self.cases["LOT-OTHER"], "SL-0001", "SL-0002"):
                self.assertIn(included, response.text, path)
            self.assertIn('<option value="all" selected>', response.text)

    def test_selection_survives_role_switch_and_review_validation_and_success(self):
        self.select("SERIES-V3")
        both = {**self.HEADERS, "x-local-roles": "Passport.Reviewer,Passport.Compliance"}
        page = self.client.post("/personas", headers=both,
                                data={"persona": "compliance", "return_to": "/cases"}).text
        self.assertIn(self.cases["LOT-B"], page)
        self.assertNotIn(self.cases["LOT-OTHER"], page)
        self.client.post("/personas", headers=both, data={"persona": "reviewer", "return_to": "/exec"})
        invalid = self.client.post(f"/holds/{self.review_id}/resolve", headers=both, data={"reason": "short"})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("BAT-201", invalid.text)
        correction = self.upload("corrected_claim", CORRECTED, "scope-correction-1")
        response = self.client.post(f"/holds/{self.review_id}/resolve", headers=both, data={
            "expected_row_version": "1", "reason": "The corrected claim matches the transport record.",
            "evidence_id": correction, "command_id": "scope-resolve-1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("BAT-201", response.text)
        self.assertEqual(self.client.cookies.get(experience.SERIES_COOKIE), "SERIES-V3")
        review = next(r for r in self.service.list_reviews() if r["review_id"] == self.review_id)
        self.assertEqual(review["status"], "resolved")

    def test_foreign_detail_and_stale_decision_form_never_escape_selected_scope(self):
        self.select("SERIES-V3")
        for path in (f"/holds/{self.other_review}", f"/cases/{self.cases['LOT-OTHER']}", "/second-life/SL-0002",
                     "/p/BAT-201", "/p/BAT-201/card", "/trail/BAT-201", "/lots/LOT-OTHER"):
            response = self.client.get(path, headers=self.HEADERS)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("Other series sourcing concern", response.text)
        correction = self.document("LOT-OTHER", "corrected_claim", "other-correction-1")
        response = self.client.post(f"/holds/{self.other_review}/resolve", headers=self.HEADERS, data={
            "expected_row_version": "1", "reason": "This form belongs to another series.",
            "evidence_id": correction, "command_id": "stale-scope-1"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(next(r for r in self.service.list_reviews() if r["review_id"] == self.other_review)["status"],
                         "open")

    def test_journey_uses_header_selection_and_changing_it_ends_the_old_tour(self):
        self.select("SERIES-V3")
        response = self.client.post("/journey", headers=self.HEADERS,
                                    data={"persona": "reviewer", "step": "hold", "series": "SERIES-V3"})
        self.assertIn('class="journey-guide', response.text)
        page = self.client.get("/journey", headers=self.HEADERS).text
        self.assertIn('id="series-select"', page)
        self.assertNotIn('id="journey-series"', page)
        self.assertEqual(form_value(page, "series"), "SERIES-V3")
        changed = self.client.get("/journey?series=SERIES-OTHER", headers=self.HEADERS)
        self.assertNotIn('class="journey-guide', changed.text)
        self.assertEqual(form_value(changed.text, "series"), "SERIES-OTHER")
        self.assertIsNone(self.client.cookies.get(experience.JOURNEY_COOKIE))
        page = self.client.get("/journey?series=all", headers=self.HEADERS).text
        self.assertIn("Choose one production series above", page)
        self.assertIn('disabled>Start journey', page)

    def test_shared_lot_is_visible_but_affected_batteries_remain_scoped(self):
        evidence = self.document("LOT-CO", "supplier_claim", "shared-claim-1")
        status, review = self.service.open_review("LOT-CO", "A concern on a shared lot", [evidence],
                                                  REVIEWER, "shared-review-1")
        self.assertEqual(status, 201)
        status, case = self.service.open_case("LOT-CO", "A shared compliance concern", [evidence],
                                              "compliance (local:co)", "shared-case-1")
        self.assertEqual(status, 201)
        for selection, included, excluded in (("SERIES-V3", "BAT-101", "BAT-201"),
                                                ("SERIES-OTHER", "BAT-201", "BAT-101")):
            self.select(selection)
            for path in ("/lots/LOT-CO", f"/holds/{review['review_id']}", f"/cases/{case['case_id']}"):
                response = self.client.get(path, headers=self.HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertIn(included, response.text)
                self.assertNotIn(excluded, response.text)

    def test_successor_stays_in_its_series_without_inflating_production_counts(self):
        with self.service.store.transaction() as tx:
            state = tx.get("PassportState", "BAT-103")
            tx.update("PassportState", {"battery_id": "BAT-103", "lifecycle_status": "second_life_pending"},
                      state["row_version"])
        status, body = self.service.decide_second_life("SL-0001", "approve", 1, "Safety assessment reviewed",
                                                       REVIEWER, "scope-approve-1")
        self.assertEqual(status, 200, body)
        page = self.select("SERIES-V3")
        self.assertIn("BAT-103-SL1", page)
        self.assertIn("3 produced batteries", page)
        self.assertIn("BAT-103-SL1", self.client.get("/", headers=self.HEADERS).text)
        self.assertNotIn("BAT-103-SL1", self.select("SERIES-OTHER"))
        self.assertIn("4 produced batteries", self.select("all"))
        self.assertEqual(exec_views.series_view(self.service, "all")["measures"]["coverage"]["produced"], 4)


class RunFigureTests(unittest.TestCase):
    PRICES = {"currency": "USD", "source": "test-list",
              "models": {"gpt-x": {"input_per_million": 2.0, "output_per_million": 8.0}}}

    @staticmethod
    def rows(execution, task_id="t1"):
        task = {"task_id": task_id, "role": "carbon", "subject": "BAT-103", "created_utc": "2026-11-11T09:00:00Z"}
        body = {"decision": {"action": "calculate", "reason": "Inputs complete"}, "outcome": {}, "execution": execution}
        return {"status_code": 200, "result_json": json.dumps(body)}, task

    def record(self, execution, prices, task_id="t1"):
        row, task = self.rows(execution, task_id)
        return exec_views.run_record(task, row, prices)

    def test_measured_run_is_labelled_ai_and_priced(self):
        record = self.record({"hosting": "hosted", "model": "gpt-x", "input_tokens": 1000, "output_tokens": 500,
                              "latency_ms": 1200}, self.PRICES)
        self.assertEqual((record["label"], record["tokens"], record["latency"], record["cost"]),
                         ("AI proposal", "1,000 in / 500 out", "1,200 ms", "USD 0.006"))

    def test_unknown_figures_stay_not_measured_never_zero(self):
        unmeasured = self.record({"hosting": "hosted", "model": "gpt-x"}, self.PRICES, "t2")
        self.assertEqual((unmeasured["tokens"], unmeasured["latency"], unmeasured["cost"]),
                         ("not measured",) * 3)
        unknown_model = self.record({"hosting": "hosted", "model": "other", "input_tokens": 1, "output_tokens": 1},
                                    self.PRICES, "t3")
        self.assertEqual(unknown_model["cost"], "not measured")
        totals = exec_views.run_totals([unmeasured], self.PRICES, 1)
        self.assertEqual((totals["tokens"], totals["cost"]["text"], totals["cost"]["per_passport"]),
                         ("tokens not measured", "not measured", "not measured"))

    def test_partial_measurement_and_missing_prices_are_explicit(self):
        measured = self.record({"hosting": "hosted", "model": "gpt-x", "input_tokens": 1000, "output_tokens": 500},
                               self.PRICES)
        unmeasured = self.record({"hosting": "hosted", "model": "gpt-x"}, self.PRICES, "t2")
        totals = exec_views.run_totals([measured, unmeasured], self.PRICES, 1)
        self.assertEqual((totals["tokens"], totals["cost"]["status"], totals["cost"]["text"]),
                         ("1,500 tokens (1 of 2 runs measured)", "partially measured", "USD 0.006"))
        unpriced = exec_views.run_totals([self.record({"hosting": "hosted", "model": "gpt-x", "input_tokens": 1,
                                                       "output_tokens": 1}, None)], None, 1)
        self.assertEqual(unpriced["cost"]["text"], "not measured")
        self.assertIn("DBP_PRICES_B64", unpriced["cost"]["note"])

    def test_scripted_local_runs_are_not_called_ai(self):
        record = self.record({"hosting": "local_scripted_double"}, None)
        self.assertEqual(record["label"], "Scripted local run (not AI)")
        self.assertFalse(record["reason_ai"])

    def test_agent_reason_is_kept_apart_from_the_application_message(self):
        task = {"task_id": "t4", "role": "supplier", "subject": "LOT-CO", "created_utc": "2026-11-11T09:00:00Z"}
        mark = {"field": "reason", "provenance_id": "PV-" + "a" * 61, "ai_generated": True}
        body = {"decision": {"action": "request_review", "reason": "Origins disagree."}, "outcome": {},
                "execution": {"hosting": "hosted"}, "provenance": [mark], "message": "Review opened."}
        record = exec_views.run_record(task, {"status_code": 200, "result_json": json.dumps(body)}, None)
        self.assertEqual((record["reason"], record["reason_ai"], record["provenance_id"], record["app_message"]),
                         ("Origins disagree.", True, mark["provenance_id"], "Review opened."))

    def test_price_settings_are_validated(self):
        encoded = base64.b64encode(json.dumps(self.PRICES).encode()).decode()
        self.assertEqual(check_prices(decode_b64_json({"DBP_PRICES_B64": encoded}, "DBP_PRICES_B64")), self.PRICES)
        self.assertEqual(check_prices({}), {})
        for bad in ({"models": {}}, {"currency": "USD", "models": {"m": {"input_per_million": -1,
                                                                          "output_per_million": 1}}},
                    {"models": {"m": {"input_per_million": 1, "output_per_million": 1}}}):
            with self.assertRaises(ValueError):
                check_prices(bad)
        with self.assertRaises(ValueError):
            decode_b64_json({"DBP_PRICES_B64": "not base64!"}, "DBP_PRICES_B64")


class LedgerTests(unittest.TestCase):
    class Probe(SqlLedger):
        def __init__(self, outcomes, block):
            self.now = 0.0
            super().__init__(None, "sql-dbp.database.windows.net", "passport", "https://digest.blob.core.windows.net",
                             clock=lambda: CHECKED, monotonic=lambda: self.now)
            self.outcomes, self.block, self.verifications = list(outcomes), block, 0

        def _verify_database(self):
            self.verifications += 1
            return self.outcomes.pop(0)

        def _record_block(self, battery_id, version):
            return self.block

    VERIFIED = {"status": "verified", "reason_code": "ok", "message": "m", "checked_utc": CHECKED,
                "performed": True, "anchored_block": 10}
    UNAVAILABLE = {"status": "unavailable", "reason_code": "no_anchored_digest", "message": "m",
                   "checked_utc": CHECKED, "performed": False}

    def test_verified_result_is_cached_and_remembered_for_public_pages(self):
        ledger = self.Probe([self.VERIFIED, self.VERIFIED], block=8)
        self.assertIsNone(ledger.last("BAT-103", 1))
        first = ledger.check("BAT-103", 1)
        self.assertEqual((first["status"], first["record_block"], first["performed"]), ("verified", 8, True))
        self.assertFalse(ledger.check("BAT-103", 1)["performed"])
        self.assertEqual(ledger.verifications, 1)
        ledger.now = 61
        self.assertTrue(ledger.check("BAT-103", 1)["performed"])
        self.assertEqual(ledger.verifications, 2)
        self.assertEqual(ledger.last("BAT-103", 1)["status"], "verified")
        self.assertIsNone(ledger.last("BAT-101", 1))

    def test_digests_are_read_only_from_the_current_database_incarnation(self):
        cursor = SimpleNamespace(execute=lambda sql: None, fetchone=lambda: (datetime(2026, 10, 23, 9, 5, 7),))
        self.assertEqual(digest_prefix("sql-dbp.database.windows.net", "passport", read_incarnation(cursor)),
                         "manual/sql-dbp/passport/20261023T090507Z/")
        ledger = self.Probe([], block=None)
        ledger.store = SimpleNamespace(credential=None)
        ledger.prefix = lambda: "manual/sql-dbp/passport/20261023T090507Z/"
        with mock.patch("azure.storage.blob.ContainerClient") as client:
            client.return_value.__enter__.return_value.list_blobs.return_value = []
            self.assertEqual(ledger._digests(), [])
        client.return_value.__enter__.return_value.list_blobs.assert_called_once_with(
            name_starts_with="manual/sql-dbp/passport/20261023T090507Z/")

    def test_version_after_the_last_anchor_or_without_a_ledger_row(self):
        self.assertEqual(self.Probe([self.VERIFIED], block=12).check("BAT-103", 1)["status"],
                         "chain_verified_anchor_pending")
        missing = self.Probe([self.VERIFIED], block=None).check("BAT-103", 1)
        self.assertEqual((missing["status"], missing["reason_code"]), ("failed", "record_missing"))

    def test_unavailable_is_never_cached_or_remembered(self):
        ledger = self.Probe([self.UNAVAILABLE, self.VERIFIED], block=8)
        self.assertEqual(ledger.check("BAT-103", 1)["status"], "unavailable")
        self.assertIsNone(ledger.last("BAT-103", 1))
        self.assertEqual(ledger.check("BAT-103", 1)["status"], "verified")
        self.assertEqual(ledger.verifications, 2)

    def test_memory_store_and_missing_digest_account_claim_nothing(self):
        memory = build_ledger(SimpleNamespace(store="memory"), None).check()
        self.assertEqual((memory["status"], memory["reason_code"]), ("unavailable", "memory_store"))
        sql = build_ledger(SimpleNamespace(store="sql", digest_account_url="http://insecure"), None).check()
        self.assertEqual(sql["reason_code"], "digest_store_not_configured")


class TelemetryTests(unittest.TestCase):
    def test_only_allowlisted_events_fields_and_codes_leave_the_process(self):
        with self.assertRaises(ValueError):
            telemetry.build("passport.viewed")
        with self.assertRaises(ValueError):
            telemetry.build("hold.resolved", reason=HOLD_REASON)
        with self.assertRaises(ValueError):
            telemetry.build("hold.resolved", reason_code=HOLD_REASON)
        with self.assertRaises(ValueError):
            telemetry.build("agent.run", latency_ms=float("nan"))
        dims, measures = telemetry.build("agent.run", role="carbon", model="gpt-x", input_tokens=None,
                                         output_tokens=None, latency_ms=900)
        self.assertEqual(measures, {"latency_ms": 900})  # unknown tokens stay missing, never zero
        self.assertEqual((dims["role"], dims["dbp.schema_version"]), ("carbon", "2"))

    def test_disabled_by_default_and_never_raises(self):
        self.assertFalse(telemetry.from_env({}).enabled)
        sent = []
        tele = telemetry.Telemetry(lambda *event: sent.append(event))
        with self.assertLogs("passport", "WARNING"):
            self.assertIsNone(tele.emit("hold.resolved", reason=HOLD_REASON))
        self.assertEqual(sent, [])


class BusinessEventTests(Screens):
    def setUp(self):
        self.sent = []
        self.build(sink=lambda name, dims, measures: self.sent.append((name, dims, measures)))

    def test_screens_emit_minimized_events_without_reason_text_or_names(self):
        self.client.get("/exec", headers=NOBODY)
        correction = self.upload("corrected_claim", CORRECTED, "ev-fix-1")
        page = self.client.get(f"/holds/{self.review_id}", headers=REV).text
        reason = "Corrected claim matches the transport record"
        self.client.post(f"/holds/{self.review_id}/resolve", headers=REV, follow_redirects=False, data={
            "evidence_id": correction, "reason": reason, "command_id": form_value(page, "command_id"),
            "expected_row_version": form_value(page, "expected_row_version")})
        names = [name for name, _, _ in self.sent]
        self.assertIn("access.denied", names)
        self.assertIn("hold.resolved", names)
        self.assertEqual(names.count("passport.published"), 2)
        denied = next(d for n, d, _ in self.sent if n == "access.denied")
        self.assertEqual((denied["route_template"], denied["actor_role"], denied["reason_code"]),
                         ("/exec", "no_role", "role_missing"))
        resolved = next(d for n, d, _ in self.sent if n == "hold.resolved")
        self.assertEqual((resolved["hold_id"], resolved["actor_role"], resolved["series_id"]),
                         (self.review_id, "reviewer", "SERIES-V3"))
        text = json.dumps(self.sent)
        for private in (reason, HOLD_REASON, "rafael", "nora", "Mine North"):
            self.assertNotIn(private, text)
class ComplianceScreenTests(Screens):
    """An adverse report on LOT-CO (used by all three batteries) and the Compliance case it leads to."""

    def setUp(self):
        self.sent = []
        self.build(sink=lambda name, dims, measures: self.sent.append((name, dims, measures)))
        self.adverse = self.post_evidence(COMP, "adverse_report", {"report_id": "ADV-1"}).json()["evidence_id"]

    def post_evidence(self, headers, kind, value, name="report.json", content_type="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        return self.client.post("/evidence", headers=headers, files={"file": (name, data, content_type)},
                                data={"kind": kind, "lot_id": "LOT-CO",
                                      "command_id": f"web-ev-{len(self.sent)}-{kind[:3]}-{len(data)}"})

    def open_case(self, headers=None):
        return self.client.post("/compliance/cases", headers=headers or COMP, json={
            "lot_id": "LOT-CO", "reason": "Adverse report names the mine", "evidence_ids": [self.adverse],
            "command_id": "web-case-0001"})

    def test_only_compliance_opens_and_decides_and_each_step_emits_a_coded_event(self):
        self.assertEqual(self.post_evidence(REV, "audit_report", {"a": 1}).status_code, 403)
        self.assertEqual(self.post_evidence(COMP, "supplier_claim", {"a": 1}).json()["error"],
                         "evidence_kind_not_allowed")
        self.assertEqual(self.post_evidence(COMP, "adverse_report", b"plain", "a.txt", "text/plain").json()["error"],
                         "evidence_not_json")
        self.assertEqual(self.open_case(REV).status_code, 403)
        case = self.open_case().json()
        self.assertEqual(self.client.post("/compliance/cases", headers=COMP, json={
            "lot_id": "LOT-CO", "reason": "Second case", "evidence_ids": [self.adverse],
            "command_id": "web-case-0002"}).json()["error"], "case_already_open")
        self.assertEqual(self.service.board()["text"], "1 of 3 passports committed")  # BAT-103 stays published
        url = f"/compliance/cases/{case['case_id']}/decisions"
        keep = {"decision": "keep_hold", "reason": "Wait for the audit", "command_id": "web-dec-0001"}
        for headers in (REV, AGENT, OP):
            self.assertEqual(self.client.post(url, headers=headers, json={**keep, "expected_row_version": 1})
                             .status_code, 403)
        self.assertEqual(self.client.post(url, headers=COMP, json=keep).json()["error"], "row_version_required")
        self.assertEqual(self.client.post(url, headers=COMP, json={**keep, "expected_row_version": 7}).status_code,
                         409)
        self.assertEqual(self.client.post(url, headers=COMP, json={**keep, "command_id": "web-dec-0002",
                                                                   "expected_row_version": 1}).status_code, 200)
        audit = self.post_evidence(COMP, "audit_report", {"audit_id": "AUD-1"}).json()["evidence_id"]
        accepted = self.client.post(url, headers=COMP, json={
            "decision": "accept_with_action_plan", "expected_row_version": 2, "owner": "Supplier quality lead",
            "due_date": "2027-01-31", "evidence_ids": [audit], "reason": "Audit answers the report",
            "command_id": "web-dec-0003"}).json()
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.post_evidence(REV, "adverse_report", {"report_id": "ADV-2"}).status_code, 201)
        detail = self.client.get(f"/compliance/cases/{case['case_id']}", headers=AUTH).json()
        self.assertEqual((detail["case"]["status"], detail["case"]["reopen_count"]), ("open", 1))
        self.assertEqual(self.client.get("/compliance/cases", headers=OP).status_code, 403)
        names = [name for name, _, _ in self.sent]
        self.assertEqual([n for n in names if n.startswith("compliance.")],
                         ["compliance.case.opened", "compliance.case.decided", "compliance.case.decided",
                          "compliance.case.reopened"])
        text = json.dumps(self.sent)
        for private in ("Supplier quality lead", "Audit answers", "Adverse report names", "carmen"):
            self.assertNotIn(private, text)

    def test_case_screens_show_the_lot_evidence_and_labels_to_case_readers_only(self):
        case_id = self.open_case().json()["case_id"]
        for path in ("/cases", f"/cases/{case_id}"):
            for headers in (COMP, REV, AUTH):
                page = self.client.get(path, headers=headers)
                self.assertEqual(page.status_code, 200, (path, headers))
                self.assertIn("LOT-CO", page.text)
                self.assertIn("Illustrative", page.text)
                self.assertIn(BANNER, page.text)
            for headers in (OP, NOBODY):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403, (path, headers))
        detail = self.client.get(f"/cases/{case_id}", headers=COMP).text
        self.assertIn('name="decision"', detail)
        self.assertIn("BAT-103", detail)
        self.assertIn("report.json", detail)
        self.assertNotIn('name="decision"', self.client.get(f"/cases/{case_id}", headers=REV).text)
        self.assertIn("Compliance cases", self.client.get("/exec", headers=REV).text)
        self.assertNotIn("Compliance cases", self.client.get("/exec", headers=OP).text)
        self.assertIn(f"Compliance case {case_id} opened", self.client.get("/trail/BAT-103", headers=REV).text)

    def next_card(self, headers=REV):
        return re.search(r'<div class="next">(.*?)</div>', self.client.get("/exec", headers=headers).text,
                         re.S).group(1)

    def test_next_step_leads_from_the_hold_to_the_case_and_back_to_readiness(self):
        case_id = self.open_case().json()["case_id"]
        self.assertIn("Review the hold on LOT-A", self.next_card())
        correction = self.upload("corrected_claim", CORRECTED, "ev-fix-1")
        status, body = self.service.resolve_review(self.review_id, 1, "Corrected claim matches transport",
                                                   [correction], REVIEWER, "cmd-res-0001")
        self.assertEqual(status, 200, body)
        card = self.next_card()
        for text in (f'href="/cases/{case_id}">Review the compliance case on LOT-CO',
                     "3 batteries stay held until a Compliance Officer decides"):
            self.assertIn(text, card)
        audit = self.post_evidence(COMP, "audit_report", {"audit_id": "AUD-1"}).json()["evidence_id"]
        done = self.client.post(f"/cases/{case_id}/decide", headers=COMP, data={
            "decision": "accept_with_action_plan", "expected_row_version": "1", "owner": "Supplier quality lead",
            "due_date": "2027-01-31", "evidence_id": audit, "reason": "Audit answers the report",
            "command_id": "web-dec-0001"})
        self.assertEqual(done.status_code, 200, done.text)
        for text in ("Decision recorded", "The agents now reassess", 'href="/exec">Back to passport readiness',
                     'href="/cases">Back to compliance cases'):
            self.assertIn(text, done.text)
        self.assertNotIn("compliance case", self.next_card())


class LotTests(Screens):
    def test_readiness_table_links_each_lot_and_battery(self):
        page = self.client.get("/exec", headers=OP).text
        self.assertIn("Open a lot to see its documents", page)
        table = page.split('<table class="lots">', 1)[1].split("</table>", 1)[0]
        self.assertIn('href="/lots/LOT-A"', table)
        for battery in ("BAT-101", "BAT-102"):
            self.assertIn(f'<a href="/p/{battery}">{battery}</a>', table)
        self.assertNotIn('href="/trail/', table)

    def test_lot_checks_each_file_against_its_recorded_hash_without_showing_content(self):
        row = next(r for r in self.service.list_evidence() if r["evidence_id"] == self.claim)
        for headers in (OP, REV, COMP):
            with self.subTest(headers=headers):
                page = self.client.get("/lots/LOT-A", headers=headers)
                self.assertEqual(page.status_code, 200)
                document = page.text.split(f'<li id="evidence-{self.claim}">', 1)[1].split("</li>", 1)[0]
                self.assertIn(row["sha256"], document)
                self.assertEqual(document.count("SHA-256 matches"), 2)
                for text in ("Current file", "Recorded version", "SHA-256 recorded in SQL"):
                    self.assertIn(text, document)
                self.assertNotIn(CLAIM["origin_site"], document)
                self.assertIn("does not prove the claim is true", page.text)

    def test_lot_reports_changed_or_missing_bytes_without_claiming_integrity(self):
        row, original = self.service.evidence_content(self.claim)
        real_get = self.blobs.get
        for current, recorded, current_text, recorded_text in (
            (b"changed", original, "SHA-256 mismatch", "SHA-256 matches"),
            (original, b"changed", "SHA-256 matches", "SHA-256 mismatch"),
            (None, None, "File missing", "Version unavailable"),
        ):
            with self.subTest(current=current_text, recorded=recorded_text):
                def get(container, path, version_id=None, current=current, recorded=recorded):
                    if container == "evidence" and path == row["blob_path"]:
                        return recorded if version_id else current
                    return real_get(container, path, version_id)

                with mock.patch.object(self.blobs, "get", side_effect=get):
                    page = self.client.get("/lots/LOT-A", headers=REV)
                self.assertEqual(page.status_code, 200)
                document = page.text.split(f'<li id="evidence-{self.claim}">', 1)[1].split("</li>", 1)[0]
                text = " ".join(re.sub(r"<[^>]+>", " ", document).split())
                self.assertIn(f"Current file: {current_text}", text)
                self.assertIn(f"Recorded version: {recorded_text}", text)
                self.assertEqual(document.count("SHA-256 matches"),
                                 int(current_text == "SHA-256 matches") + int(recorded_text == "SHA-256 matches"))

    def test_reviewer_sees_documents_batteries_and_holds_of_a_lot(self):
        page = self.client.get("/lots/LOT-A", headers=REV)
        self.assertEqual(page.status_code, 200)
        for text in ("supplier_claim.json", "transport_record.json", "Complete (2 of 2)", BANNER, "Illustrative",
                     'href="/trail/BAT-101"', 'href="/trail/BAT-102"', f'href="/holds/{self.review_id}"',
                     f'href="/evidence/{self.claim}/content"', 'href="/exec">Back to passport readiness'):
            self.assertIn(text, page.text)
        main = page.text.split('<main id="main-content">', 1)[1].split("</main>", 1)[0]
        self.assertNotIn("BAT-103", main)

    def test_operator_sees_file_names_but_cannot_read_content(self):
        page = self.client.get("/lots/LOT-A", headers=OP).text
        self.assertIn("supplier_claim.json", page)
        self.assertNotIn("/evidence/", page)
        self.assertIn("Only a Passport.Reviewer or Passport.Compliance can read document content", page)
        self.assertEqual(self.client.get(f"/evidence/{self.claim}/content", headers=OP).status_code, 403)

    def test_compliance_can_open_a_lot_and_read_its_documents(self):
        page = self.client.get("/lots/LOT-A", headers=COMP).text
        self.assertIn(f'href="/evidence/{self.claim}/content"', page)
        self.assertIn('href="/cases">Back to compliance cases', page)
        for headers in (COMP, REV):
            content = self.client.get(f"/evidence/{self.claim}/content", headers=headers)
            self.assertEqual(content.status_code, 200, headers)
            self.assertEqual(json.loads(content.content), CLAIM)
            self.assertIn("attachment", content.headers["content-disposition"])

    def test_a_lot_missing_its_transport_record_says_so(self):
        self.assertIn("No claim or transport (0 of 2)", self.client.get("/lots/LOT-B", headers=OP).text)
        status, body = self.service.upload_evidence("supplier_claim", "LOT-B", "claim-b.json", "application/json",
                                                    json.dumps(CLAIM).encode(), REVIEWER, "ev-claim-b")
        self.assertEqual(status, 201, body)
        for path in ("/exec", "/lots/LOT-B"):
            page = self.client.get(path, headers=OP).text
            self.assertIn("Claim only (1 of 2)", page, path)
            self.assertIn("Transport record missing", page, path)
        self.assertIn('href="/lots/LOT-B"', self.client.get("/exec", headers=OP).text)

    def test_unknown_lots_and_other_roles_are_refused(self):
        unknown = self.client.get("/lots/LOT-ZZ", headers=REV)
        self.assertEqual(unknown.status_code, 404)
        self.assertIn("This supplier lot does not exist.", unknown.text)
        self.assertEqual(self.client.get("/lots/LOT-A").status_code, 401)
        for headers in (NOBODY, AUTH, AGENT):
            self.assertEqual(self.client.get("/lots/LOT-A", headers=headers).status_code, 403, headers)


if __name__ == "__main__":
    unittest.main()
