"""Exercise the orchestrator (dbp_mvp) against a fake Azure CLI.

No sign-in, no Azure calls and nothing deployed: these tests show the command sequence and the safety checks,
not that Azure accepts them.
"""

import base64
import contextlib
import dataclasses
import io
import itertools
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbp_mvp import MvpError, bootstrap, cli, config, devbox, fabric, preflight, progress, teardown  # noqa: E402
from dbp_mvp.az import Azure, AzError  # noqa: E402
from dbp_mvp.state import State, StateError, fingerprint  # noqa: E402

SUBSCRIPTION = "00000000-0000-0000-0000-00000000000a"
TENANT = "00000000-0000-0000-0000-0000000000b0"
OPERATOR = "00000000-0000-0000-0000-0000000000c0"
UPN = "operator@contoso.example"
CLIENT_IP = "20.50.0.10"
REQUIRED = {"subscription_id": SUBSCRIPTION.upper(), "operator_upn": UPN, "prefix": "mvp01", "client_ip": CLIENT_IP}
GIT = {("remote", "get-url", "origin"): "git@github.com:contoso/passport.git",
       ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): "origin/main",
       ("rev-parse", "--show-prefix"): "mvp/"}
CAPACITY = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/mvp01-rg/providers/Microsoft.Fabric/capacities/mvp01fabric"
ACCOUNT = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/mvp01-rg/providers/Microsoft.CognitiveServices/accounts/"
           "mvp01-ai")
OUTPUTS = {"vmPublicIp": {"type": "String", "value": "20.240.0.10"},
           "fabricCapacityId": {"type": "String", "value": CAPACITY},
           "appParameters": {"type": "Object", "value": {"prefix": "mvp01"}}}


def setUpModule():
    progress.print = lambda *args, **kwargs: None  # silence the progress lines


def tearDownModule():
    del progress.print


def make_config(**overrides):
    return config.parse({**REQUIRED, **overrides})


def fake_git(*args):
    return GIT.get(args)


class Fail:
    def __init__(self, stderr="ERROR: (ResourceNotFound) The resource was not found.", code=1):
        self.stderr = stderr
        self.code = code


NOT_FOUND = Fail()


class Raw(str):
    """Plain text output, for az.text()."""


class Seq:
    """Successive responses; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)

    def next(self):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class FakeAzure(Azure):
    """Answers az commands from regex routes over the joined arguments and records every call and @file."""

    def __init__(self, routes=()):
        super().__init__(SUBSCRIPTION)
        self.routes, self.calls, self.files = [], [], []
        for pattern, response in routes:
            self.on(pattern, response)

    def on(self, pattern, response):
        self.routes.insert(0, (re.compile(pattern), response))  # later routes win
        return self

    def _execute(self, command, capture=True):
        args = command[:command.index("--only-show-errors")]
        joined = " ".join(args)
        self.calls.append(joined)
        for arg in args:
            if arg.startswith("@") and Path(arg[1:]).is_file():
                self.files.append((joined, Path(arg[1:]), Path(arg[1:]).read_text(encoding="utf-8")))
        for pattern, response in self.routes:
            if pattern.search(joined):
                if isinstance(response, Seq):
                    response = response.next()
                if callable(response) and not isinstance(response, (Fail, Raw)):
                    response = response(args)
                if isinstance(response, Fail):
                    return response.code, "", response.stderr
                if response is None:
                    return 0, "", ""
                return 0, response if isinstance(response, Raw) else json.dumps(response), ""
        raise AssertionError(f"unexpected az call: {joined}")

    def called(self, pattern):
        return [call for call in self.calls if re.search(pattern, call)]

    def index(self, pattern):
        return next(i for i, call in enumerate(self.calls) if re.search(pattern, call))

    def file(self, pattern):
        return next(content for call, _, content in self.files if re.search(pattern, call))


def session_routes(upn=UPN, subscription=SUBSCRIPTION, user_type="user"):
    return [(r"^account show", {"id": subscription, "tenant": TENANT, "name": "Sandbox",
                                "user": {"name": upn, "type": user_type}}),
            (r"^ad signed-in-user show", {"id": OPERATOR, "upn": upn})]


class Workspace(unittest.TestCase):
    """Temporary state file and captured output."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp(prefix="dbp-mvp-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.state = State(folder / "state.json")
        self.lines = []
        self.enterContext(mock.patch.object(config, "_git", fake_git))
        self.enterContext(mock.patch("dbp_mvp.fabric.time.sleep"))

    def out(self, text=""):
        self.lines.append(text)

    @property
    def printed(self):
        return "\n".join(self.lines)


class ConfigTests(unittest.TestCase):
    def test_example_lists_every_setting_and_only_needs_the_required_values(self):
        text = (ROOT / "config" / "mvp.example.toml").read_text(encoding="utf-8")
        for key in (*config.REQUIRED, *config.DEFAULTS, *config.SCENARIOS):
            self.assertRegex(text, rf"(?m)^#? ?{key} =")
        raw = tomllib.loads(text)
        with self.assertRaises(config.ConfigError):
            config.parse(raw)
        filled = config.parse({**raw, **REQUIRED})
        self.assertEqual(filled, make_config())
        self.assertEqual(filled.subscription_id, SUBSCRIPTION)
        self.assertEqual(filled.models, config.DEFAULT_MODELS)

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.parse({**REQUIRED, "prefix": "MVP-1", "client_ip": "10.1.2.3", "colour": "blue",
                          "enable_alerts": True, "extra_batteries": "5", "repo_url": "http://example.com/x.git",
                          "scenarios": {"evaluation": "yes", "chaos": True},
                          "models": [{"name": "gpt-5.4", "version": "2026-03-05", "capacity": 0},
                                     {"name": "gpt-5.4", "version": "2026-03-05", "capacity": 5, "tier": "x"}]})
        message = str(caught.exception)
        for expected in ("unknown setting(s): colour", "prefix must be", "client_ip must be",
                         "alert_email is required", "extra_batteries must be whole number", "repo_url must be",
                         "scenarios.evaluation", "unknown scenario: chaos", "models #1: capacity",
                         "models #2: unknown key(s) tier", "names must be unique"):
            self.assertIn(expected, message)

    def test_obsolete_free_offer_is_not_silently_replaced_with_paid_compute(self):
        with self.assertRaisesRegex(config.ConfigError, "Remove use_free_sql_offer.*paid General Purpose"):
            config.parse({**REQUIRED, "use_free_sql_offer": True})

    def test_load_accepts_a_byte_order_mark_and_explains_a_missing_file(self):
        folder = Path(tempfile.mkdtemp(prefix="dbp-mvp-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = folder / "mvp.toml"
        path.write_text("\ufeff" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in REQUIRED.items()),
                        encoding="utf-8")
        self.assertEqual(config.load(path).prefix, "mvp01")
        with self.assertRaisesRegex(config.ConfigError, "Copy config/mvp.example.toml"):
            config.load(folder / "absent.toml")

    def test_repository_comes_from_the_checkout_without_credentials(self):
        with mock.patch.object(config, "_git", fake_git):
            self.assertEqual(make_config().repository(),
                             config.Repository("https://github.com/contoso/passport.git", "main", "mvp"))
            self.assertEqual(make_config(repo_url="https://example.com/fork.git", repo_ref="v1").repository(),
                             config.Repository("https://example.com/fork.git", "v1", "mvp"))
        self.assertEqual(config._https_origin("https://user:token@github.com/contoso/passport.git"),
                         "https://github.com/contoso/passport.git")
        self.assertEqual(config._https_origin("C:/repositories/passport"), "")
        unsafe = {**GIT, ("remote", "get-url", "origin"): "https://github.com/contoso/pass port.git",
                  ("rev-parse", "--show-prefix"): "../outside/"}
        with mock.patch.object(config, "_git", lambda *args: unsafe.get(args)):
            self.assertEqual(make_config().repository(), config.Repository("", "", ""))

    def test_settings_copy_round_trips_and_holds_no_secret(self):
        settings = make_config(deploy_fabric=False, scenarios={"evaluation": True},
                               models=[{"name": "gpt-5.4", "version": "2026-03-05", "capacity": 20}])
        repository = config.Repository("https://github.com/contoso/passport.git", "main", "mvp")
        copy = config.parse(tomllib.loads(settings.to_toml(repository)))
        self.assertEqual(copy, dataclasses.replace(settings, repo_url=repository.url, repo_ref="main"))
        self.assertEqual(copy.models[0]["sku"], "DataZoneStandard")
        self.assertNotRegex(json.dumps(settings.as_dict()), "(?i)password|secret")

    def test_prices_come_from_a_file_next_to_the_settings_and_travel_inline(self):
        folder = Path(tempfile.mkdtemp(prefix="dbp-mvp-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        price = {"input_per_million": 1.25, "output_per_million": 10}
        (folder / "prices.json").write_text(json.dumps({"currency": " USD ", "models": {
            "gpt-5.4": price, "gpt-5.4-mini": {"input_per_million": 0, "output_per_million": 2}}}), encoding="utf-8")
        settings = config.parse({**REQUIRED, "prices_file": "prices.json"}, path=folder / "mvp.toml")
        self.assertEqual(settings.prices, {"currency": "USD", "models": {
            "gpt-5.4": price, "gpt-5.4-mini": {"input_per_million": 0, "output_per_million": 2}}})
        repository = config.Repository("https://github.com/contoso/passport.git", "main", "mvp")
        copy = config.parse(tomllib.loads(settings.to_toml(repository)))
        self.assertEqual(copy, dataclasses.replace(settings, repo_url=repository.url, repo_ref="main", prices_file=""))
        self.assertEqual(make_config().prices, {})

        (folder / "broken.json").write_text("{", encoding="utf-8")
        for raw, expected in (({"prices_file": "prices.json", "prices": {}}, ["set either prices_file or prices"]),
                              ({"prices_file": "absent.json"}, ["cannot be read"]),
                              ({"prices_file": "broken.json"}, ["is not valid JSON"]),
                              ({"prices": {"currency": "", "tier": 1, "models": {"GPT 5": price, "gpt-x": {
                                  "input_per_million": -1, "output_per_million": 1}}}},
                               ["unknown key(s) tier", "currency must be", "'GPT 5' is not a model name",
                                "gpt-x needs exactly input_per_million"])):
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError) as caught:
                config.parse({**REQUIRED, **raw}, path=folder / "mvp.toml")
            for text in expected:
                self.assertIn(text, str(caught.exception))

    def test_price_file_override_is_relative_to_current_directory_and_does_not_rewrite_settings(self):
        with tempfile.TemporaryDirectory(prefix="dbp-mvp-test-") as directory:
            folder = Path(directory)
            settings_file = folder / "config" / "mvp.toml"
            settings_file.parent.mkdir()
            old = {"currency": "USD", "models": {
                "gpt-6.1-sol": {"input_per_million": 1, "output_per_million": 2}}}
            new = {**old, "source": "updated list"}
            document = make_config(prices=old).to_toml().replace(
                'prices_file = ""', 'prices_file = "no-longer-present.json"')
            settings_file.write_text(document, encoding="utf-8")
            price_file = folder / "prices.json"
            price_file.write_text(json.dumps(new), encoding="utf-8-sig")
            with contextlib.chdir(folder):
                loaded = config.load(settings_file, prices_file="prices.json")
            self.assertEqual(loaded.prices, new)
            self.assertEqual(Path(loaded.prices_file), price_file)
            self.assertEqual(settings_file.read_text(encoding="utf-8"), document)

            new["models"]["gpt-6.1-sol"]["input_per_million"] = 4
            price_file.write_text(json.dumps(new), encoding="utf-8")
            self.assertEqual(config.load(settings_file, prices_file=price_file).prices, new)
            for content in ('{"currency": "USD", "models": {}}', '{"currency":', json.dumps({
                    **new, "models": {"gpt-6.1-sol": {"input_per_million": -1, "output_per_million": 2}}})):
                price_file.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(config.ConfigError):
                    config.load(settings_file, prices_file=price_file)
            with self.assertRaisesRegex(config.ConfigError, "cannot be read"):
                config.load(settings_file, prices_file=folder / "missing.json")


class StateTests(Workspace):
    def test_phases_persist_and_skip_only_with_the_same_inputs(self):
        self.state.bind(make_config().identity)
        self.state.record("A1", "abc", {"vmPublicIp": "20.240.0.10"})
        reloaded = State(self.state.path)
        self.assertTrue(reloaded.done("A1", "abc"))
        self.assertFalse(reloaded.done("A1", "changed"))
        self.assertEqual(reloaded.outputs("A1"), {"vmPublicIp": "20.240.0.10"})
        reloaded.record("A2", "x")
        reloaded.record("A3", "y")
        reloaded.forget_from("A2", bootstrap.ORDER)
        self.assertEqual(set(State(self.state.path).data["phases"]), {"A1"})
        with self.assertRaises(StateError):
            reloaded.forget_from("B9", bootstrap.ORDER)

    def test_state_belongs_to_one_environment_and_never_stores_secrets(self):
        self.state.bind(make_config().identity)
        with self.assertRaisesRegex(StateError, "belongs to prefix 'mvp01'"):
            State(self.state.path).bind(make_config(prefix="other").identity)
        with self.assertRaises(StateError):
            self.state.record("A1", "abc", {"app": {"clientSecret": "x"}})
        self.assertNotIn("A1", State(self.state.path).data["phases"])
        self.state.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "unreadable"):
            State(self.state.path)
        self.assertEqual(fingerprint({"b": 1, "a": 2}), fingerprint({"a": 2, "b": 1}))


class AzureCliTests(unittest.TestCase):
    def test_refuses_shell_metacharacters_and_long_commands_before_running(self):
        az = FakeAzure()
        for value in ("a&b", "x|y", "50%", 'say "hi"', "line\nbreak"):
            with self.assertRaises(ValueError):
                az("group", "show", "--name", value)
        with self.assertRaises(ValueError):
            az("group", "show", "--name", "x" * 9000)
        self.assertEqual(az.calls, [])

    def test_find_returns_none_only_when_the_resource_is_missing(self):
        az = FakeAzure([(r"^group show --name gone", NOT_FOUND),
                        (r"^group show --name denied", Fail("ERROR: (AuthorizationFailed) no access")),
                        (r"^group exists", None)])
        self.assertIsNone(az.find("group", "show", "--name", "gone"))
        with self.assertRaises(AzError) as caught:
            az.find("group", "show", "--name", "denied")
        self.assertFalse(caught.exception.not_found)
        self.assertIsNone(az("group", "exists", "--name", "x"))

    def test_bodies_and_parameters_travel_in_temporary_files(self):
        az = FakeAzure([(r"^rest", {"ok": True}), (r"^deployment", {})])
        self.assertEqual(az.rest("put", "https://management.azure.com/x", {"value": "body"}), {"ok": True})
        with az.parameter_file({"vmAdminPassword": "Secret-Value-1"}) as reference:
            az("deployment", "sub", "create", "--parameters", reference)
        (rest_call, rest_path, rest_body), (deploy_call, deploy_path, document) = az.files
        self.assertIn("--headers Content-Type=application/json --body @", rest_call)
        self.assertEqual(json.loads(rest_body), {"value": "body"})
        self.assertEqual(json.loads(document)["parameters"], {"vmAdminPassword": {"value": "Secret-Value-1"}})
        self.assertNotIn("Secret-Value-1", deploy_call)
        self.assertFalse(rest_path.exists() or deploy_path.exists())


class PreflightTests(unittest.TestCase):
    def test_pure_checks(self):
        self.assertEqual(preflight.version_tuple("2.91.0"), (2, 91, 0))
        self.assertLess(preflight.version_tuple("2.9.1"), preflight.MIN_AZ)
        subscription = f"/subscriptions/{SUBSCRIPTION}"
        group = "/providers/Microsoft.Management/managementGroups/root"
        role = preflight.has_required_roles
        self.assertTrue(role([{"role": "Owner", "scope": subscription}], SUBSCRIPTION))
        self.assertTrue(role([{"role": "Contributor", "scope": subscription},
                              {"role": "User Access Administrator", "scope": group}], SUBSCRIPTION))
        self.assertFalse(role([{"role": "Contributor", "scope": subscription}], SUBSCRIPTION))
        self.assertFalse(role([{"role": "Owner", "scope": subscription + "/resourceGroups/other"}], SUBSCRIPTION))
        allowed = {"defaultUserRolePermissions": {"allowedToCreateApps": True}}
        denied = {"value": [{"defaultUserRolePermissions": {"allowedToCreateApps": False}}]}
        developer = [{"roleTemplateId": "CF1C38E5-3621-4004-A7CB-879624DCED7C"}]
        self.assertTrue(preflight.can_create_apps(allowed, None))
        self.assertFalse(preflight.can_create_apps(denied, []))
        self.assertTrue(preflight.can_create_apps(denied, developer))
        self.assertIsNone(preflight.can_create_apps(None, None))
        self.assertTrue(preflight.can_grant_app_roles([{"roleTemplateId": "62E90394-69F5-4237-9190-012177145E10"}]))
        self.assertFalse(preflight.can_grant_app_roles(developer))
        self.assertIsNone(preflight.can_grant_app_roles(None))
        required = preflight.required_providers(make_config(vm_access="jit"))
        self.assertIn("Microsoft.Fabric", required)
        self.assertIn("Microsoft.Security", required)
        self.assertNotIn("Microsoft.Fabric", preflight.required_providers(make_config(deploy_fabric=False)))
        self.assertEqual(preflight.missing_providers(["Microsoft.App", "Microsoft.Sql"],
                                                     [{"n": "microsoft.app", "s": "Registered"},
                                                      {"n": "Microsoft.Sql", "s": "Registering"}]),
                         ["Microsoft.Sql"])
        self.assertIsNone(preflight.image_problem({"properties": {"hyperVGeneration": "V2", "architecture": "x64"}}))
        self.assertIn("Generation 2", preflight.image_problem({"hyperVGeneration": "V1"}))
        self.assertIn("not offered", preflight.size_problem([], "swedencentral"))
        self.assertIn("restricted", preflight.size_problem(
            [{"name": config.VM_SIZE, "restrictions": [{"type": "Location", "reasonCode": "NotAvailable"}]}],
            "swedencentral"))
        usages = [{"name": {"value": "OpenAI.DataZoneStandard.gpt-5.4"}, "limit": 30, "currentValue": 25}]
        models = [{"name": "gpt-5.4", "sku": "DataZoneStandard", "capacity": 10},
                  {"name": "gpt-5.4-mini", "sku": "DataZoneStandard", "capacity": 10}]
        errors, warnings = preflight.quota_shortfalls(models, usages, {})
        self.assertEqual((len(errors), len(warnings)), (1, 1))
        self.assertEqual(preflight.quota_shortfalls(models[:1], usages, {"gpt-5.4": 10}), ([], []))

    def test_session_must_match_the_configuration(self):
        settings = make_config()
        self.assertEqual(preflight.session_check(settings, FakeAzure(session_routes(upn=UPN.upper()))),
                         {"tenant_id": TENANT, "operator_object_id": OPERATOR, "subscription_name": "Sandbox"})
        for routes, message in ((session_routes(subscription="11111111-1111-1111-1111-111111111111"),
                                 "az account set"),
                                (session_routes(user_type="servicePrincipal"), "as a user"),
                                (session_routes(upn="someone@contoso.example"), "operator_upn")):
            with self.assertRaisesRegex(preflight.PreflightError, message):
                preflight.session_check(settings, FakeAzure(routes))

    def _azure(self):
        registered = [{"n": name, "s": "Registered"} for name in preflight.PROVIDERS]
        return FakeAzure(session_routes() + [
            (r"^version$", {"azure-cli": "2.91.0"}),
            (r"^bicep version", Raw("Bicep CLI version 0.49.2 (0a1b2c3d4e)")),
            (r"^bicep upgrade", Raw("")),
            (r"^cloud show", "AzureCloud"),
            (r"^role assignment list", [{"role": "Owner", "scope": f"/subscriptions/{SUBSCRIPTION}"}]),
            (r"authorizationPolicy", {"defaultUserRolePermissions": {"allowedToCreateApps": True}}),
            (r"transitiveMemberOf", {"value": [{"roleTemplateId": "158c047a-c907-4556-b7ef-446551a6b5f7"}]}),
            (r"api\.fabric\.microsoft\.com/v1/workspaces", {"value": []}),
            (r"^provider list", Seq(registered + [{"n": "Microsoft.Fabric", "s": "NotRegistered"}],
                                    registered + [{"n": "Microsoft.Fabric", "s": "Registered"}])),
            (r"^provider register", None),
            (r"^vm image show", {"hyperVGeneration": "V2", "architecture": "x64"}),
            (r"^vm list-skus", [{"name": config.VM_SIZE, "restrictions": []}]),
            (r"^group show", NOT_FOUND),
            (r"^cognitiveservices usage list",
             [{"name": {"value": f"OpenAI.DataZoneStandard.{m['name']}"}, "limit": 333, "currentValue": 0}
              for m in config.DEFAULT_MODELS])])

    def test_run_registers_missing_providers_and_returns_the_identity(self):
        az = self._azure()
        result = preflight.run(make_config(), az, out=lambda text: None)
        self.assertEqual((result["tenant_id"], result["operator_object_id"]), (TENANT, OPERATOR))
        self.assertEqual(az.called(r"^provider register"), ["provider register --namespace Microsoft.Fabric"])

    def test_a_missing_role_for_the_agent_grants_warns_without_blocking(self):
        warnings = []
        preflight.run(make_config(), self._azure().on(r"transitiveMemberOf", {"value": []}), out=warnings.append)
        self.assertTrue(any("Cloud Application Administrator" in warning and "B4" in warning
                            for warning in warnings))

    def test_alerts_management_is_registered_once_even_with_alerts_disabled(self):
        namespace = "Microsoft.AlertsManagement"
        for enabled in (False, True):
            with self.subTest(enable_alerts=enabled):
                settings = make_config(enable_alerts=enabled, alert_email="alerts@contoso.example")
                registered = [{"n": name, "s": "Registered"} for name in preflight.required_providers(settings)
                              if name != namespace]
                az = self._azure().on(r"^provider list", Seq(
                    registered + [{"n": namespace, "s": "NotRegistered"}],
                    registered + [{"n": namespace, "s": "Registered"}]))
                preflight.run(settings, az, out=lambda text: None)
                self.assertEqual(az.called(r"^provider register"), [f"provider register --namespace {namespace}"])
                self.assertLess(az.index(r"^provider register"), az.index(r"^vm image show"))
                az.calls.clear()
                preflight.run(settings, az, out=lambda text: None)
                self.assertEqual(az.called(r"^provider register"), [])

    def test_bicep_older_than_the_template_api_versions_is_upgraded(self):
        for installed, upgrades in (("Bicep CLI version 0.38.0 (5a6b7c8d9e)", ["bicep upgrade"]),
                                    ("Bicep CLI version 0.49.2 (0a1b2c3d4e)", [])):
            az = self._azure().on(r"^bicep version", Raw(installed))
            preflight.run(make_config(), az, out=lambda text: None)
            self.assertEqual(az.called(r"^bicep upgrade"), upgrades)

    def test_registrations_start_together_and_are_awaited_together(self):
        az = FakeAzure([(r"^provider register", None),
                        (r"^provider list", Seq([{"n": "Microsoft.Sql", "s": "Registering"}],
                                                [{"n": "Microsoft.Sql", "s": "Registered"}],
                                                [{"n": "Microsoft.Sql", "s": "Registered"},
                                                 {"n": "Microsoft.App", "s": "Registered"}]))])
        lines, waits = [], []
        preflight.register_providers(az, ["Microsoft.App", "Microsoft.Sql"], [
            {"n": "Microsoft.Sql", "s": "Registering"}], out=lines.append, sleep=waits.append)
        self.assertEqual(az.calls[0], "provider register --namespace Microsoft.App")
        self.assertEqual(len(az.called(r"^provider register")), 1)  # an ongoing registration is only awaited
        self.assertEqual(waits, [preflight.REGISTRATION_POLL] * 2)
        self.assertEqual(lines, ["Registered Microsoft.Sql; 1 still registering.", "Registered Microsoft.App."])
        az.on(r"^provider list", [])
        with self.assertRaisesRegex(preflight.PreflightError, "still registering after 2 minutes: Microsoft.App"):
            preflight.register_providers(az, ["Microsoft.App"], [], sleep=lambda seconds: None, timeout=120)

    def test_fabric_not_activated_blocks_before_anything_is_created(self):
        fabric_call = r"api\.fabric\.microsoft\.com/v1/workspaces"
        refused = Fail("ERROR: Unauthorized({\"errorCode\":\"PowerBINotLicensedException\"})")
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.run(make_config(), self._azure().on(fabric_call, refused), out=lambda text: None)
        self.assertIn("https://app.fabric.microsoft.com", str(caught.exception))
        self.assertIn("deploy_fabric = false", str(caught.exception))

        warnings = []
        preflight.run(make_config(), self._azure().on(fabric_call, Fail("ERROR: connection reset")),
                      out=warnings.append)
        self.assertTrue(any("Fabric" in warning for warning in warnings))

        az = self._azure().on(fabric_call, refused)
        preflight.run(make_config(deploy_fabric=False), az, out=lambda text: None)
        self.assertEqual(az.called(fabric_call), [])

    def test_run_reports_every_blocking_problem_together(self):
        az = self._azure()
        az.on(r"^role assignment list", [{"role": "Contributor", "scope": f"/subscriptions/{SUBSCRIPTION}"}])
        az.on(r"^cognitiveservices usage list",
              [{"name": {"value": "OpenAI.DataZoneStandard.gpt-5.6-terra"}, "limit": 5, "currentValue": 0}])
        az.on(r"^group show", {"location": "westeurope", "tags": {}})
        az.on(r"^cognitiveservices account list", [])
        warnings = []
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.run(make_config(), az, out=warnings.append)
        message = str(caught.exception)
        for expected in ("Owner", "OpenAI.DataZoneStandard.gpt-5.6-terra", "no project=battery-passport tag",
                         "is in westeurope"):
            self.assertIn(expected, message)
        self.assertEqual(len(warnings), 2)  # no quota entry for the two other models


class Cloud:
    """A fake subscription for the bootstrap: the VM exists once the deployment has run."""

    def __init__(self, settings, vm_exists=False):
        self.vm_exists = vm_exists
        self.marker = "tools=ready clone=cloned venv=ready"
        machine = {"id": devbox.vm_id(settings), "created": "2026-11-12T08:00:00+00:00"}
        self.az = FakeAzure(session_routes() + [
            (r"^vm show .*--query id$", lambda args: machine["id"] if self.vm_exists else NOT_FOUND),
            (r"^vm show .*--query \{id:id", machine),
            (r"^deployment sub show", NOT_FOUND),
            (r"^deployment sub create", self._deploy),
            (r"^deployment sub what-if", None),
            (r"^resource list .*Microsoft.Fabric/capacities", []),
            (r"^rest --method get --url .*capacities/",
             Seq({"properties": {"state": "Active"}}, {"properties": {"state": "Paused"}})),
            (r"^rest --method post --url .*/suspend", None),
            (r"^vm get-instance-view", ["ProvisioningState/succeeded", "PowerState/running"]),
            (r"^vm run-command invoke", lambda args: {"value": [
                {"code": "ComponentStatus/StdOut/succeeded", "message": f"Installing\nDBP_MVP_DEVBOX: {self.marker}"},
                {"code": "ComponentStatus/StdErr/succeeded", "message": ""}]}),
            (r"^network nsg rule show", {"one": f"{settings.client_ip}/32", "many": None}),
            (r"^network nsg rule update", None)])

    def _deploy(self, args):
        self.vm_exists = True
        return OUTPUTS


class BootstrapTests(Workspace):
    def setUp(self):
        super().setUp()
        self.settings = make_config()
        self.cloud = Cloud(self.settings)
        self.preflight = self.enterContext(mock.patch.object(preflight, "run", return_value={
            "tenant_id": TENANT, "operator_object_id": OPERATOR, "subscription_name": "Sandbox", "warnings": []}))

    def run_bootstrap(self, settings=None, **options):
        bootstrap.run(settings or self.settings, self.cloud.az, self.state, out=self.out,
                      **{"yes": True, **options})

    def test_first_run_creates_the_vm_and_shows_the_password_without_storing_it(self):
        self.run_bootstrap()
        az = self.cloud.az
        parameters = json.loads(az.file(r"^deployment sub create"))["parameters"]
        password = parameters["vmAdminPassword"]["value"]
        self.assertTrue(parameters["createDevboxVm"]["value"])
        self.assertEqual(self.printed.count(password), 2)  # after A1 and in the final summary
        self.assertNotIn(password, self.state.path.read_text(encoding="utf-8"))
        self.assertNotIn(password, " ".join(az.calls))
        self.assertEqual(set(self.state.data["phases"]), set(bootstrap.ORDER))
        self.assertEqual(self.state.outputs("A1")["vmPublicIp"], "20.240.0.10")
        self.assertEqual(len(az.called(r"^rest --method post --url .*mvp01fabric/suspend")), 1)
        self.assertLess(az.index(r"^deployment sub create"), az.index(r"^vm run-command"))

        invoke = az.called(r"^vm run-command invoke")[0]
        self.assertIn(f"--scripts @{bootstrap.SCRIPT}", invoke)
        self.assertIn("--parameters RepoUrl=https://github.com/contoso/passport.git RepoRef=main Subfolder=mvp "
                      "ConfigBase64=", invoke)
        encoded = re.search(r"ConfigBase64=(\S+)", invoke).group(1)
        copy = config.parse(tomllib.loads(base64.b64decode(encoded).decode("utf-8")))
        self.assertEqual(copy, dataclasses.replace(self.settings, repo_url="https://github.com/contoso/passport.git",
                                                   repo_ref="main"))
        for expected in ("address  20.240.0.10", f"user     {config.VM_ADMIN}", f"az login --tenant {TENANT}",
                         rf"cd {config.VM_CHECKOUT}\mvp", "python -m dbp_mvp deploy"):
            self.assertIn(expected, self.printed)

    def test_unchanged_rerun_skips_deployment_and_setup(self):
        self.run_bootstrap()
        self.cloud.az.calls.clear()
        self.lines.clear()
        self.run_bootstrap()
        self.assertEqual(self.preflight.call_count, 1)
        self.assertEqual(self.cloud.az.called(r"^deployment sub create|^vm run-command"), [])
        for phase in ("A0 preflight", "A1 infrastructure", "A2 developer VM"):
            self.assertIn(f"{phase}: unchanged, skipped", self.printed)
        self.assertNotIn("Password:", self.printed)

    def test_changed_setting_redeploys_without_replacing_the_vm(self):
        self.run_bootstrap()
        self.lines.clear()
        changed = dataclasses.replace(self.settings, vm_auto_shutdown="18:00")
        self.run_bootstrap(changed)
        documents = [json.loads(content) for call, _, content in self.cloud.az.files
                     if call.startswith("deployment sub create")]
        second = documents[1]["parameters"]
        self.assertFalse(second["createDevboxVm"]["value"])
        self.assertEqual(second["vmAutoShutdownTime"]["value"], "18:00")
        self.assertNotIn(second["vmAdminPassword"]["value"], self.printed)
        self.assertIn("vm reset-password", self.printed)
        self.assertEqual(len(self.cloud.az.called(r"^vm run-command")), 2)  # the settings copy changed

    def test_from_repeats_the_named_phase_and_later_ones(self):
        self.run_bootstrap()
        self.run_bootstrap(start="A2")
        self.assertEqual(len(self.cloud.az.called(r"^deployment sub create")), 1)
        self.assertEqual(len(self.cloud.az.called(r"^vm run-command")), 2)

    def test_declining_or_a_running_deployment_deploys_nothing(self):
        with self.assertRaisesRegex(MvpError, "Cancelled"):
            self.run_bootstrap(yes=False, confirm=lambda prompt: "no")
        self.cloud.az.on(r"^deployment sub show", {"state": "Running", "location": "swedencentral"})
        with self.assertRaisesRegex(MvpError, "still running"):
            self.run_bootstrap()
        self.assertEqual(self.cloud.az.called(r"^deployment sub create"), [])
        self.assertFalse(self.state.done("A1"))

    def test_failed_vm_setup_points_to_the_log_and_is_not_recorded(self):
        for marker, message in (("", "did not report a result"), ("tools=failed clone=skipped venv=skipped",
                                                                   "Tool installation failed")):
            self.cloud.marker = marker
            with self.assertRaisesRegex(MvpError, message) as caught:
                self.run_bootstrap()
            self.assertIn(bootstrap.SETUP_LOG, str(caught.exception))
            self.assertFalse(self.state.done("A2"))

    def test_clone_failure_prints_the_manual_steps_and_access_is_reset(self):
        self.cloud.marker = "tools=ready clone=failed venv=skipped"
        self.cloud.az.on(r"^network nsg rule show", {"one": None, "many": ["20.50.0.99/32"]})
        self.run_bootstrap()
        self.assertIn(f"git clone <repository URL> {config.VM_CHECKOUT}", self.printed)
        self.assertEqual(self.cloud.az.called(r"^network nsg rule update"),
                         ["network nsg rule update --resource-group mvp01-rg --nsg-name mvp01-devbox-nsg --name "
                          f"AllowRdpFromClient --source-address-prefixes {CLIENT_IP}/32"])

    def test_access_recreates_an_rdp_rule_removed_outside_the_template(self):
        self.cloud.az.on(r"^network nsg rule show", NOT_FOUND).on(r"^network nsg rule create", None)
        self.run_bootstrap()
        self.assertEqual(len(self.cloud.az.called(r"^network nsg rule create")), 1)
        self.assertTrue(self.state.done("A3"))

    def test_subscription_and_tenant_prerequisite_failures_explain_the_remedy(self):
        stderr = ('{"code":"SubscriptionNotRegisteredForFeature","message":"Subscription is not registered for '
                  'feature Microsoft.Network/AllowBringYourOwnPublicIpAddress required to carry out the requested '
                  'operation."} {"code":"BadRequest","message":"Tenant \'MetadataLocator: [TenantId=x]\' wasn\'t '
                  'recognized by Microsoft Fabric. Sign up for Microsoft Fabric and try again."}')
        self.cloud.az.on(r"^deployment sub create", Fail(stderr))
        with self.assertRaises(MvpError) as caught:
            self.run_bootstrap()
        message = str(caught.exception)
        for expected in ("az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress",
                         "support request", "https://app.fabric.microsoft.com", "deploy_fabric = false",
                         bootstrap.RERUN):
            self.assertIn(expected, message)
        self.assertFalse(self.state.done("A1"))

        self.cloud.az.on(r"^deployment sub create", Fail("ERROR: InvalidTemplate"))
        with self.assertRaises(AzError) as caught:
            self.run_bootstrap()
        self.assertNotIn(bootstrap.RERUN, str(caught.exception))

    def test_what_if_previews_and_records_nothing(self):
        self.run_bootstrap(what_if=True)
        self.assertEqual(len(self.cloud.az.called(r"^deployment sub what-if --name mvp01-mvp ")), 1)
        self.assertEqual(self.cloud.az.called(r"^deployment sub create|^vm run-command"), [])
        self.assertFalse(self.state.path.exists())

    def test_pending_dns_link_explains_wait_without_retrying_the_deployment(self):
        stderr = ('{"code":"Conflict","message":"Another operation is pending for requested object.",'
                  '"target":"Microsoft.Network/privateDnsZones/privatelink.services.ai.azure.com/'
                  'virtualNetworkLinks/mvp01-link"}')
        self.cloud.az.on(r"^deployment sub create", Fail(stderr))
        with self.assertRaises(MvpError) as caught:
            self.run_bootstrap()
        message = str(caught.exception)
        for expected in (stderr, "Wait", "Azure Support", "Do not delete", "bootstrap --from A0"):
            self.assertIn(expected, message)
        self.assertEqual(len(self.cloud.az.called(r"^deployment sub create")), 1)
        self.assertEqual(self.cloud.az.called(r"^vm run-command"), [])
        self.assertFalse(self.state.done("A1"))

    def test_paused_capacity_is_resumed_for_the_update_and_paused_again(self):
        self.cloud.az.on(r"^resource list .*Microsoft.Fabric/capacities", [CAPACITY])
        self.cloud.az.on(r"^rest --method get --url .*capacities/",
                         Seq({"properties": {"state": "Paused"}}, {"properties": {"state": "Paused"}},
                             {"properties": {"state": "Active"}}, {"properties": {"state": "Active"}},
                             {"properties": {"state": "Paused"}}))
        self.cloud.az.on(r"^rest --method post --url .*/resume\?", None)
        self.run_bootstrap()
        az = self.cloud.az
        self.assertLess(az.index(r"^rest --method post --url .*mvp01fabric/resume"),
                        az.index(r"^deployment sub create"))
        self.assertLess(az.index(r"^deployment sub create"), az.index(r"^rest --method post --url .*/suspend"))

    def test_failed_deployment_pauses_the_resumed_capacity_and_explains_a_duplicate_role(self):
        self.cloud.az.on(r"^resource list .*Microsoft.Fabric/capacities", [CAPACITY])
        self.cloud.az.on(r"^rest --method get --url .*capacities/",
                         Seq({"properties": {"state": "Paused"}}, {"properties": {"state": "Paused"}},
                             {"properties": {"state": "Active"}}, {"properties": {"state": "Active"}},
                             {"properties": {"state": "Paused"}}))
        self.cloud.az.on(r"^rest --method post --url .*/resume\?", None)
        self.cloud.az.on(r"^deployment sub create", Fail(
            '{"code":"RoleAssignmentExists","message":"The role assignment already exists. The ID of the '
            'existing role assignment is 00000000000040008000000000000abc."}'))
        with self.assertRaises(MvpError) as caught:
            self.run_bootstrap()
        message = str(caught.exception)
        for expected in ("[?name=='00000000-0000-4000-8000-000000000abc']", "az role assignment delete",
                         bootstrap.RERUN):
            self.assertIn(expected, message)
        self.assertEqual(len(self.cloud.az.called(r"^rest --method post --url .*mvp01fabric/suspend")), 1)
        self.assertFalse(self.state.done("A1"))

    def test_parse_marker_uses_the_last_report(self):
        text = ("DBP_MVP_DEVBOX: tools=failed clone=skipped venv=skipped\n"
                "DBP_MVP_DEVBOX: tools=ready clone=existing venv=ready")
        self.assertEqual(bootstrap.parse_marker(text), {"tools": "ready", "clone": "existing", "venv": "ready"})
        self.assertIsNone(bootstrap.parse_marker("DBP_MVP_DEVBOX: tools=maybe"))


class DevboxTests(Workspace):
    NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)

    def test_generated_passwords_meet_windows_complexity(self):
        for _ in range(200):
            value = devbox.generate_password()
            self.assertEqual(len(value), 20)
            self.assertTrue(value[0].isalpha())
            for check in (str.islower, str.isupper, str.isdigit, lambda c: c in devbox.SYMBOLS):
                self.assertTrue(any(check(c) for c in value))
            self.assertFalse(set(value) & set('&|<>^%"'))

    def test_start_only_when_not_running(self):
        az = FakeAzure([(r"^vm get-instance-view", ["ProvisioningState/succeeded", "PowerState/deallocated"]),
                        (r"^vm start", None)])
        devbox.start(az, make_config(), out=self.out)
        self.assertEqual(len(az.called(r"^vm start")), 1)
        az.on(r"^vm get-instance-view", ["PowerState/running"])
        devbox.ensure_running(az, make_config())
        self.assertEqual(len(az.called(r"^vm start")), 1)

    def test_source_ip_access_updates_the_rule_and_rejects_private_addresses(self):
        az = FakeAzure([(r"^network nsg rule show", "AllowRdpFromClient"), (r"^network nsg rule update", None)])
        devbox.access(az, make_config(), "20.50.0.20", out=self.out)
        self.assertTrue(az.calls[1].endswith("--source-address-prefixes 20.50.0.20/32"))
        self.assertIn('Set client_ip = "20.50.0.20"', self.printed)
        with self.assertRaisesRegex(MvpError, "not a public IPv4"):
            devbox.access(az, make_config(), "10.0.0.4")
        self.assertEqual(len(az.calls), 2)

    def test_source_ip_access_recreates_a_removed_rule(self):
        az = FakeAzure([(r"^network nsg rule show", NOT_FOUND), (r"^network nsg rule create", None)])
        devbox.access(az, make_config(), out=self.out)
        self.assertEqual(az.called(r"^network nsg rule update"), [])
        created = az.called(r"^network nsg rule create")[0]
        for part in ("--name AllowRdpFromClient", "--priority 100", "--access Allow", "--direction Inbound",
                     f"--source-address-prefixes {CLIENT_IP}/32", "--destination-port-ranges 3389"):
            self.assertIn(part, created)
        self.assertIn("was missing and has been recreated", self.printed)

    def test_jit_access_updates_the_policy_and_requests_a_window(self):
        settings = make_config(vm_access="jit")
        policy = {"kind": "Basic", "properties": {"virtualMachines": [
            {"id": devbox.vm_id(settings).upper(), "ports": [
                {"number": 3389, "protocol": "*", "allowedSourceAddressPrefix": "20.50.0.99",
                 "maxRequestAccessDuration": "PT3H"}]}]}}
        az = FakeAzure([(r"^rest --method get --url .*jitNetworkAccessPolicies/default\?", policy),
                        (r"^rest --method (put|post)", {})])
        devbox.access(az, settings, out=self.out, now=self.NOW)
        updated = json.loads(az.file(r"--method put"))
        self.assertEqual(updated["properties"]["virtualMachines"][0]["ports"][0]["allowedSourceAddressPrefix"],
                         CLIENT_IP)
        request = json.loads(az.file(r"/initiate\?api-version=2020-01-01"))
        self.assertEqual(request["virtualMachines"][0]["ports"],
                         [{"number": 3389, "allowedSourceAddressPrefix": CLIENT_IP,
                           "endTimeUtc": "2026-11-12T12:00:00Z"}])
        policy["properties"]["virtualMachines"] = []
        with self.assertRaisesRegex(MvpError, "does not cover RDP"):
            devbox.access(az, settings, out=self.out, now=self.NOW)

    def test_password_reset_passes_the_password_in_a_deleted_file(self):
        az = FakeAzure([(r"^vm get-instance-view", ["PowerState/running"]), (r"^vm user update", None)])
        password = devbox.reset_password(az, make_config(), out=self.out)
        call, path, content = az.files[0]
        self.assertEqual(content, password)
        self.assertNotIn(password, call)
        self.assertIn(f"--username {config.VM_ADMIN} --password @", call)
        self.assertFalse(path.exists())
        self.assertIn(password, self.printed)


class FabricTests(unittest.TestCase):
    def test_capacity_resumes_and_waits_or_times_out(self):
        az = FakeAzure([(r"^rest --method get", Seq({"properties": {"state": "Paused"}},
                                                    {"properties": {"state": "Resuming"}},
                                                    {"properties": {"state": "Active"}})),
                        (r"^rest --method post --url .*/resume\?", None)])
        waits = []
        self.assertEqual(fabric.set_capacity(az, CAPACITY, active=True, sleep=waits.append), "Active")
        self.assertEqual(waits, [15, 15])
        az.on(r"^rest --method get", {"properties": {"state": "Resuming"}})
        with self.assertRaisesRegex(MvpError, "did not become Active"):
            fabric.set_capacity(az, CAPACITY, active=True, sleep=lambda seconds: None, timeout=30)

    def test_workspace_lookup_follows_pages_and_ownership_needs_the_marker(self):
        az = FakeAzure([(r"^rest --method get --url https://api.fabric.microsoft.com/v1/workspaces",
                         {"value": [{"id": "w1", "displayName": "other"}], "continuationToken": "t1"}),
                        (r"continuationToken=t1", {"value": [{"id": "w2", "displayName": "mvp01-passport"}]})])
        self.assertEqual(fabric.find_workspace(az, "mvp01-passport")["id"], "w2")
        self.assertIn("--resource https://api.fabric.microsoft.com", az.calls[0])
        self.assertTrue(fabric.owned({"description": f"Synthetic data ({config.MARKER})"}))
        self.assertFalse(fabric.owned({"description": None}))
        az.on(r"workspaces --resource https://api.fabric.microsoft.com$", {"value": [], "continuationToken": "a%2Bb"})
        with self.assertRaisesRegex(MvpError, "could not be paged"):
            fabric.find_workspace(az, "absent")


class TeardownTests(Workspace):
    DELETES = r"delete|purge"
    MISSING_CONTAINER = Fail("ERROR: (ContainerOperationFailure) The specified container does not exist.", code=3)

    def setUp(self):
        super().setUp()
        self.settings = make_config()
        self.state.bind(self.settings.identity)
        self.state.record("A1", "abc")
        self.clock = itertools.count(0, 60)
        self.az = FakeAzure(session_routes() + [
            (r"^group show", {"location": "swedencentral", "tags": {"project": config.PROJECT_TAG}}),
            (r"^storage account list", ["mvp01ledger"]),
            (r"^storage container immutability-policy show", {"state": "Unlocked"}),
            (r"^storage container immutability-policy delete", None),
            (r"^rest --method get --url https://api.fabric.microsoft.com/v1/workspaces",
             {"value": [{"id": "w1", "displayName": "mvp01-passport", "description": f"({config.MARKER})"}]}),
            (r"^rest --method delete --url https://api.fabric.microsoft.com/v1/workspaces/w1", None),
            (r"^cognitiveservices account list --", [{"id": ACCOUNT, "name": "mvp01-ai"}]),
            (r"^rest --method delete --url .*/projects/passport/capabilityHosts/agents", None),
            (r"^rest --method get --url .*/projects/passport/capabilityHosts/agents",
             Seq({"properties": {"provisioningState": "Deleting"}}, NOT_FOUND)),
            (r"^rest --method delete --url .*/projects/passport\?", None),
            (r"^rest --method get --url .*/projects/passport\?",
             Seq({"properties": {"provisioningState": "Deleting"}}, NOT_FOUND)),
            (r"^rest --method get --url .*/mvp01-ai/capabilityHosts\?", {"value": []}),
            (r"^cognitiveservices account delete", None),
            (r"^cognitiveservices account purge", None),
            (r"^network vnet subnet show", Seq([{"id": "link"}], [])),
            (r"^group delete", None),
            (r"^deployment sub delete", None),
            (r"^ad app list", [{"appId": "a1", "name": "mvp01-passport", "tags": [config.MARKER]}]),
            (r"^ad app delete", None),
            (r"^group exists", False),
            (r"^cognitiveservices account list-deleted", [])])

    def run_teardown(self, **options):
        teardown.run(self.settings, self.az, self.state, out=self.out, sleep=lambda seconds: None,
                     clock=lambda: next(self.clock), **{"yes": True, **options})

    def test_deletes_in_dependency_order_and_clears_state(self):
        self.run_teardown()
        order = [self.az.index(pattern) for pattern in (
            r"^rest --method delete --url https://api.fabric", r"^storage container immutability-policy delete",
            r"^rest --method delete --url .*capabilityHosts/agents",
            r"^rest --method delete --url .*/projects/passport\?", r"^cognitiveservices account delete",
            r"^cognitiveservices account purge", r"^group delete", r"^deployment sub delete", r"^ad app delete")]
        self.assertEqual(order, sorted(order))
        host_reads = self.az.called(r"^rest --method get --url .*/projects/passport/capabilityHosts/agents")
        self.assertEqual(len(host_reads), 2)
        self.assertLess(max(i for i, call in enumerate(self.az.calls) if call in host_reads),
                        self.az.index(r"^rest --method delete --url .*/projects/passport\?"))
        self.assertIn("--if-match *", self.az.called(r"immutability-policy delete")[0])
        self.assertEqual(len(self.az.called(r"^network vnet subnet show")), 2)
        self.assertFalse(self.state.path.exists())
        self.assertIn("Nothing from this MVP remains", self.printed)

    def test_missing_containers_are_skipped_but_existing_policies_are_removed(self):
        self.az.on(r"^storage account list", ["mvp01app", "mvp01ledger"])
        self.az.on(r"^storage container immutability-policy show .*--account-name mvp01app ",
                   self.MISSING_CONTAINER)
        self.run_teardown()
        deletes = self.az.called(r"^storage container immutability-policy delete")
        self.assertEqual(len(deletes), 1)
        self.assertIn("--account-name mvp01ledger ", deletes[0])
        self.assertEqual(len(self.az.called(r"^group delete")), 1)
        self.assertFalse(self.state.path.exists())
        self.assertIn("Teardown complete.", self.printed)

    def test_missing_container_does_not_hide_locked_or_unreadable_retention(self):
        self.az.on(r"^storage account list", ["mvp01app", "mvp01ledger"])
        self.az.on(r"^storage container immutability-policy show .*--account-name mvp01app ",
                   self.MISSING_CONTAINER)
        failures = [
            ({"state": "Locked"}, MvpError, "Locked retention policy on mvp01ledger"),
            (Fail("ERROR: (ContainerOperationFailure) This request is not authorized.", code=3),
             AzError, "This request is not authorized"),
            (Fail("ERROR: (AuthorizationFailed) Access denied.", code=3), AzError, "Access denied"),
        ]
        for response, error_type, message in failures:
            with self.subTest(message=message):
                self.az.on(r"^storage container immutability-policy show .*--account-name mvp01ledger ", response)
                with self.assertRaisesRegex(error_type, message):
                    self.run_teardown()
                self.assertEqual(self.az.called(self.DELETES), [])
                self.assertTrue(self.state.path.exists())

    def test_container_removed_after_retention_check_is_already_gone(self):
        self.az.on(r"^storage container immutability-policy delete", self.MISSING_CONTAINER)
        self.run_teardown()
        self.assertEqual(len(self.az.called(r"^group delete")), 1)
        self.assertFalse(self.state.path.exists())
        self.assertIn("Teardown complete.", self.printed)

    def test_locked_retention_or_foreign_group_stops_before_any_delete(self):
        refuse = mock.Mock(side_effect=AssertionError("must not ask"))
        self.az.on(r"^storage container immutability-policy show", {"state": "Locked"})
        with self.assertRaisesRegex(MvpError, "Locked retention policy on mvp01ledger"):
            self.run_teardown(yes=False, confirm=refuse)
        self.az.on(r"^group show", {"tags": {"project": "something-else"}})
        with self.assertRaisesRegex(MvpError, "not created by this MVP"):
            self.run_teardown(yes=False, confirm=refuse)
        self.assertEqual(self.az.called(self.DELETES), [])
        self.assertTrue(self.state.path.exists())

    def test_wrong_confirmation_cancels(self):
        with self.assertRaisesRegex(MvpError, "Cancelled"):
            self.run_teardown(yes=False, confirm=lambda prompt: "mvp01")
        self.assertEqual(self.az.called(self.DELETES), [])

    def test_objects_not_created_here_are_kept_and_reported(self):
        self.az.on(r"^ad app list", [{"appId": "a2", "name": "mvp01-passport", "tags": []}])
        self.az.on(r"^rest --method get --url https://api.fabric.microsoft.com/v1/workspaces",
                   {"value": [{"id": "w1", "displayName": "mvp01-passport", "description": "team space"}]})
        with self.assertRaisesRegex(MvpError, "Teardown incomplete") as caught:
            self.run_teardown()
        self.assertIn("app registration mvp01-passport (a2)", str(caught.exception))
        self.assertIn("Fabric workspace mvp01-passport: left in place", str(caught.exception))
        self.assertEqual(self.az.called(r"^ad app delete|--url https://api.fabric.microsoft.com/v1/workspaces/w1"),
                         [])
        self.assertEqual(len(self.az.called(r"^group delete")), 1)

    def test_paused_capacity_is_resumed_once_to_delete_the_workspace(self):
        self.az.on(r"^rest --method delete --url https://api.fabric.microsoft.com/v1/workspaces/w1",
                   Seq(Fail("ERROR: Bad Request({\"errorCode\":\"CapacityNotActive\"})"), None))
        self.az.on(r"^resource list", [CAPACITY])
        self.az.on(r"^rest --method get --url .*capacities/",
                   Seq({"properties": {"state": "Paused"}}, {"properties": {"state": "Active"}}))
        self.az.on(r"^rest --method post --url .*/resume\?", None)
        self.run_teardown()
        self.assertEqual(len(self.az.called(r"^rest --method delete --url https://api.fabric")), 2)
        self.assertIn("Deleted Fabric workspace mvp01-passport.", self.printed)

    def test_capability_host_that_never_goes_stops_before_the_account_is_deleted(self):
        self.az.on(r"^rest --method get --url .*/projects/passport/capabilityHosts/agents",
                   {"properties": {"provisioningState": "Deleting"}})
        with self.assertRaisesRegex(MvpError, "capability host on mvp01-ai was not removed"):
            self.run_teardown()
        self.assertEqual(self.az.called(
            r"^rest --method delete --url .*/projects/passport\?|^cognitiveservices account delete|^group delete"), [])

    def test_account_delete_waits_until_azure_reports_the_project_missing(self):
        project_missing = False

        def project_state(args):
            nonlocal project_missing
            project_missing = len(self.az.called(r"^rest --method get --url .*/projects/passport\?")) > 1
            return NOT_FOUND if project_missing else {"properties": {"provisioningState": "Deleting"}}

        self.az.on(r"^rest --method get --url .*/projects/passport\?", project_state)
        self.az.on(r"^cognitiveservices account delete", lambda args: None if project_missing else
                   Fail("ERROR: (CannotDeleteResource) Cannot delete resource while nested resources exist."))
        self.run_teardown()
        self.assertTrue(project_missing)
        self.assertIn("Teardown complete.", self.printed)

    def test_project_cleanup_failure_preserves_the_account_and_progress(self):
        for method, response, error_type, message in (
            ("get", {"properties": {"provisioningState": "Deleting"}}, MvpError,
             "project passport.*within 15 minutes"),
            ("get", Fail("ERROR: (AuthorizationFailed) Project access denied."), AzError, "Project access denied"),
            ("delete", Fail("ERROR: (AuthorizationFailed) Project deletion denied."), AzError,
             "Project deletion denied"),
        ):
            with self.subTest(message=message):
                self.az.on(rf"^rest --method {method} --url .*/projects/passport\?", response)
                with self.assertRaisesRegex(error_type, message):
                    self.run_teardown()
                self.assertEqual(self.az.called(r"^cognitiveservices account (delete|purge)|^group delete"), [])
                self.assertTrue(self.state.path.exists())

    def test_project_and_host_already_deleted_allow_teardown_to_resume(self):
        self.az.on(r"^rest --method delete --url .*/projects/passport(?:/capabilityHosts/agents)?\?", NOT_FOUND)
        self.az.on(r"^rest --method get --url .*/projects/passport(?:/capabilityHosts/agents)?\?", NOT_FOUND)
        self.run_teardown()
        self.assertEqual(len(self.az.called(r"^cognitiveservices account delete")), 1)
        self.assertFalse(self.state.path.exists())

    def test_account_capability_host_is_awaited_before_deleting_the_account(self):
        host_id = f"{ACCOUNT}/capabilityHosts/service"
        self.az.on(r"^rest --method get --url .*/mvp01-ai/capabilityHosts\?", {"value": [{"id": host_id}]})
        self.az.on(r"^rest --method delete --url .*/capabilityHosts/service\?", None)
        self.az.on(r"^rest --method get --url .*/capabilityHosts/service\?",
                   Seq({"properties": {"provisioningState": "Deleting"}}, NOT_FOUND))
        self.run_teardown()
        reads = self.az.called(r"^rest --method get --url .*/capabilityHosts/service\?")
        self.assertEqual(len(reads), 2)
        self.assertLess(max(i for i, call in enumerate(self.az.calls) if call in reads),
                        self.az.index(r"^cognitiveservices account delete"))


class SetupScriptTests(unittest.TestCase):
    SCRIPT = ROOT / "dbp_mvp" / "devbox-setup.ps1"

    def test_every_reported_result_is_understood_by_the_bootstrap(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn('Write-Output "DBP_MVP_DEVBOX: tools=$tools clone=$clone venv=$venv"', text)
        self.assertIn("Get-AuthenticodeSignature", text)
        values = {}
        for name, value in re.findall(r"\$(tools|clone|venv) = '(\w+)'", text):
            values.setdefault(name, set()).add(value)
        self.assertEqual(set(values), {"tools", "clone", "venv"})
        for tools, clone, venv in itertools.product(values["tools"], values["clone"], values["venv"]):
            self.assertEqual(bootstrap.parse_marker(f"DBP_MVP_DEVBOX: tools={tools} clone={clone} venv={venv}"),
                             {"tools": tools, "clone": clone, "venv": venv})

    def test_every_download_is_version_pinned_and_hash_checked(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        pins = re.findall(r"(\w+)\s*=\s*@\{\s*Url\s*=\s*'(https://[^']+)'\s*Sha256\s*=\s*'([^']*)'\s*\}", text)
        self.assertEqual({name for name, _, _ in pins}, {"Git", "Python", "AzCli", "Azd", "Bicep"})
        for name, url, sha256 in pins:
            self.assertRegex(sha256, r"^[0-9a-f]{64}$", name)
            self.assertRegex(url, r"\d+\.\d+\.\d+", name)
            self.assertNotRegex(url, r"(?i)latest|stable|aka\.ms", name)
        self.assertEqual(len(re.findall(r"'https://", text)), len(pins), "an unpinned download URL is in the script")
        self.assertIn("Get-FileHash -LiteralPath $file -Algorithm SHA256", text)
        self.assertNotIn("bicep', 'install'", text)

    def test_script_parses_in_windows_powershell(self):
        shell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not shell:
            self.skipTest("Windows PowerShell is not installed")
        check = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{self.SCRIPT}', [ref]$null, [ref]$errors); "
                 "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 }")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CliTests(Workspace):
    def main(self, *argv):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(list(argv))
        return code, errors.getvalue()

    def test_requires_python_314(self):
        with mock.patch.object(cli.sys, "version_info", (3, 13, 99)):
            code, errors = self.main("status")
        self.assertEqual(code, 1)
        self.assertIn("Python 3.14 or later is required", errors)

    def test_missing_settings_file_is_a_clear_error(self):
        code, errors = self.main("--config", str(self.state.path.with_name("absent.toml")), "status")
        self.assertEqual(code, 1)
        self.assertIn("Copy config/mvp.example.toml", errors)

    def test_laptop_needs_no_packages(self):
        """Stage A promises only Python and Azure CLI: -S leaves out every installed package."""
        absent = self.state.path.with_name("absent.toml")
        result = subprocess.run([sys.executable, "-E", "-S", "-m", "dbp_mvp", "--config", str(absent), "bootstrap",
                                 "--what-if"], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Copy config/mvp.example.toml", result.stderr)

    def test_vm_commands_refuse_another_subscription(self):
        az = FakeAzure(session_routes(subscription="11111111-1111-1111-1111-111111111111"))
        with mock.patch.object(cli, "load", return_value=make_config()), \
                mock.patch.object(cli, "Azure", return_value=az), mock.patch.object(cli, "State",
                                                                                     return_value=self.state):
            code, errors = self.main("vm", "stop")
        self.assertEqual(code, 1)
        self.assertIn("az account set", errors)
        self.assertEqual(az.called(r"^vm "), [])


if __name__ == "__main__":
    unittest.main()
