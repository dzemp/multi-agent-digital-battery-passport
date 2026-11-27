"""The B4 building blocks of dbp_mvp.foundry against a fake Foundry project and a fake Azure CLI.

No agent is built and no Microsoft Graph or Azure call is made: the tests show what would be uploaded, reused,
granted and refused, not that Foundry or Microsoft Entra ID accepts it.
"""

import io
import itertools
import json
import socket
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_deploy import (AGENT_CLIENTS, AGENT_PRINCIPALS, APP_ID, APP_OBJECT, FOUNDRY_ACCOUNT, GRAPH, INFRA,
                         MODEL_DEPLOYMENTS, ROLES, SDK, SP, AppRoles, Foundry, agent_client, private_address)
from test_orchestrator import FakeAzure

from dbp_mvp import MvpError, foundry, identity

REGISTRATION = {"display_name": "mvp01-passport", "object_id": APP_OBJECT, "app_id": APP_ID,
                "service_principal_id": SP}
PRINCIPAL = AGENT_PRINCIPALS["supplier"]
ENVIRONMENT = foundry.hosted_environment("supplier", ROLES["supplier"]["model"], "passport.example", APP_ID)


def quiet(*_):
    pass


class PackageTests(unittest.TestCase):
    def test_the_zip_is_reproducible_and_carries_the_contracts(self):
        code, digest = foundry.package()
        self.assertEqual(foundry.package(), (code, digest))
        with zipfile.ZipFile(io.BytesIO(code)) as archive:
            entries = archive.infolist()
            self.assertTrue(archive.read("requirements.txt").strip())
            self.assertIsNone(archive.testzip())
        names = [entry.filename for entry in entries]
        self.assertEqual(names, ["main.py", "passport/__init__.py", "passport/agent_contracts.py",
                                 "passport/domain.py", "requirements.txt"])
        self.assertEqual({entry.date_time for entry in entries}, {foundry.ZIP_DATE})
        for entry in entries:
            self.assertTrue(stat.S_ISREG(entry.external_attr >> 16), entry.filename)

    def test_line_endings_do_not_change_the_package_but_content_does(self):
        code, digest = foundry.package()
        with tempfile.TemporaryDirectory() as folder:
            for name in foundry.HOSTED_FILES:
                text = (foundry.HOSTED_SOURCE / name).read_bytes().replace(b"\r\n", b"\n")
                (Path(folder) / name).write_bytes(text.replace(b"\n", b"\r\n"))
            with mock.patch.object(foundry, "HOSTED_SOURCE", Path(folder)):
                self.assertEqual(foundry.package(), (code, digest))
                with (Path(folder) / "main.py").open("ab") as source:
                    source.write(b"# changed\r\n")
                self.assertNotEqual(foundry.package()[1], digest)


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.project, self.sleeps, self.printed = Foundry(), [], []
        self.code, self.digest = foundry.package()

    def ensure(self, environment=ENVIRONMENT, digest=None, clock=lambda: 0):
        return foundry.ensure_hosted(self.project, SDK, "supplier", environment, self.code, digest or self.digest,
                                     sleep=self.sleeps.append, clock=clock, out=self.printed.append)

    def test_uploads_once_waits_for_the_build_and_reuses_it(self):
        self.assertEqual(self.ensure(), {"name": "dbp-supplier", "version": "1", "principal_id": PRINCIPAL,
                                         "client_id": AGENT_CLIENTS["supplier"], "endpoint": None})
        definition = self.project.versions["dbp-supplier"][0].definition
        self.assertEqual(definition.environment_variables, ENVIRONMENT)
        self.assertEqual(definition.code_configuration.entry_point, ["python", "main.py"])
        self.assertEqual(definition.code_configuration.dependency_resolution, "remote_build")
        self.assertEqual([record.protocol for record in definition.protocol_versions], ["responses"])
        self.assertEqual(self.sleeps, [foundry.POLL_INTERVAL])

        self.assertEqual(self.ensure()["version"], "1")
        self.assertEqual((self.project.uploads, self.sleeps), (["dbp-supplier"], [foundry.POLL_INTERVAL]))

    def test_changed_source_or_settings_upload_a_new_version(self):
        self.ensure()
        self.assertEqual(self.ensure(digest="0" * 64)["version"], "2")
        moved = {**ENVIRONMENT, "PASSPORT_APP_URL": "https://elsewhere.example"}
        self.assertEqual(self.ensure(environment=moved)["version"], "3")
        self.assertEqual(self.project.uploads, ["dbp-supplier"] * 3)

    def test_a_failed_build_without_details_does_not_guess_a_network_cause(self):
        self.project.after_build = "failed"
        with self.assertRaisesRegex(MvpError, "(?s)version 1 as failed.*Foundry returned no error details") as raised:
            self.ensure()
        self.assertIn("docs/deployment.md#hosted-agent-build-failures-and-logs", str(raised.exception))
        self.assertNotIn("enable_nat_gateway", str(raised.exception))

    def test_a_failed_build_reports_an_error_not_modeled_as_an_sdk_attribute(self):
        class Version(dict):
            version = "2"
            status = "failed"

        error = {"code": "CodeError", "message": "No Python dependency manifest found at the project root.\n"
                 "Add a 'requirements.txt' and re-deploy. See https://aka.ms/hostedagents/tsg/code"}
        with self.assertRaisesRegex(MvpError, "dbp-supplier version 2 as failed") as raised:
            foundry.wait_active(self.project, "dbp-supplier", Version(error=error), sleep=self.sleeps.append,
                                clock=lambda: 0, out=self.printed.append)
        self.assertIn(f"CodeError: {error['message']}", str(raised.exception))
        self.assertNotIn("enable_nat_gateway", str(raised.exception))
        self.assertEqual(self.sleeps, [])

    def test_a_build_that_never_finishes_times_out(self):
        self.project.after_build = "creating"
        clock = itertools.count(0, 300)
        with self.assertRaisesRegex(MvpError, "not ready after 20 minutes") as raised:
            self.ensure(clock=lambda: next(clock))
        self.assertIn("docs/deployment.md#hosted-agent-build-failures-and-logs", str(raised.exception))
        self.assertNotIn("enable_nat_gateway", str(raised.exception))
        self.assertEqual(self.sleeps, [foundry.POLL_INTERVAL] * 3)


class PromptTests(unittest.TestCase):
    def test_a_prompt_agent_changes_only_with_its_contract(self):
        project, contracts = Foundry(), foundry.contracts()
        self.assertEqual(foundry.ensure_prompt(project, SDK, contracts, "carbon"),
                         {"name": "dbp-carbon", "version": "1"})
        definition = project.versions["dbp-carbon"][0].definition
        self.assertEqual(definition.model, ROLES["carbon"]["model"])
        self.assertEqual([vars(tool) for tool in definition.tools], [contracts.tool_definition("carbon")])
        self.assertEqual(foundry.ensure_prompt(project, SDK, contracts, "carbon")["version"], "1")

        changed = SimpleNamespace(ROLES=contracts.ROLES, tool_definition=contracts.tool_definition,
                                  instructions=lambda role: contracts.instructions(role) + " Be brief.")
        self.assertEqual(foundry.ensure_prompt(project, SDK, changed, "carbon")["version"], "2")
        self.assertEqual(project.created, ["dbp-carbon", "dbp-carbon"])


class Assignments:
    """The app role assignments a service principal holds."""

    def __init__(self):
        self.held = []

    def get(self, args):
        return {"value": self.held}

    def post(self, args):
        self.held.append(json.loads(Path(args[args.index("--body") + 1][1:]).read_text(encoding="utf-8")))


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.roles, self.assignments = AppRoles(), Assignments()
        assignments = rf"{GRAPH}/servicePrincipals/{PRINCIPAL}/appRoleAssignments"
        self.az = FakeAzure([
            (rf"--method get --url {GRAPH}/applications/{APP_OBJECT} --url-parameters", self.roles.read),
            (rf"--method patch --url {GRAPH}/applications/{APP_OBJECT}", self.roles.patch),
            (rf"--method get --url {assignments}", self.assignments.get),
            (rf"--method post --url {assignments}", self.assignments.post),
            (rf"--method get --url {GRAPH}/servicePrincipals/{PRINCIPAL} --url-parameters", agent_client),
        ])

    def authorize(self, client_id=AGENT_CLIENTS["supplier"], principal=PRINCIPAL):
        return foundry.authorize(self.az, REGISTRATION, "supplier", principal, client_id, sleep=quiet)

    def test_adds_one_application_role_and_assignment_and_keeps_other_roles(self):
        operator = {"id": "11111111-0000-0000-0000-000000000001", "value": "Passport.Operator",
                    "allowedMemberTypes": ["User"]}
        self.roles.roles = [operator]
        self.assertEqual(self.authorize(), AGENT_CLIENTS["supplier"])
        self.assertEqual(self.authorize(), AGENT_CLIENTS["supplier"])

        role_id = identity.stable_id(APP_ID, "Passport.Agent.Supplier", [])
        self.assertEqual(self.roles.roles[0], operator)
        self.assertEqual([(role["id"], role["allowedMemberTypes"]) for role in self.roles.roles[1:]],
                         [(role_id, ["Application"])])
        self.assertEqual(self.assignments.held, [{"principalId": PRINCIPAL, "resourceId": SP, "appRoleId": role_id}])
        self.assertEqual((len(self.az.called(r"--method patch")), len(self.az.called(r"--method post"))), (1, 1))

    def test_refuses_a_user_role_a_disagreeing_client_and_a_malformed_identity(self):
        with self.assertRaisesRegex(MvpError, "not an object ID"):
            self.authorize(principal="agent-identity")
        self.assertEqual(self.az.calls, [])
        with self.assertRaisesRegex(MvpError, "disagree"):
            self.authorize(client_id=AGENT_CLIENTS["lifecycle"])
        self.roles.roles = [{"id": "11111111-0000-0000-0000-000000000002", "value": "Passport.Agent.Supplier",
                             "allowedMemberTypes": ["User"]}]
        with self.assertRaisesRegex(MvpError, "other members than applications"):
            self.authorize()


class AccountTests(unittest.TestCase):
    def routes(self, account=FOUNDRY_ACCOUNT, deployments=MODEL_DEPLOYMENTS):
        return FakeAzure([(r"^cognitiveservices account show", account),
                          (r"^cognitiveservices account deployment list", deployments)])

    def test_a_private_injected_account_with_the_contract_models_passes(self):
        foundry.verify_models(self.routes(), "mvp01-rg", INFRA, ROLES, private_address)

    def test_every_problem_is_reported_together(self):
        account = {"properties": {"provisioningState": "Succeeded", "publicNetworkAccess": "Enabled",
                                  "networkInjections": []}}
        deployments = json.loads(json.dumps(MODEL_DEPLOYMENTS))
        deployments[0]["properties"]["model"]["version"] = "2025-01-01"
        deployments[1]["properties"]["provisioningState"] = "Creating"

        def public(host, port, proto=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("52.1.2.3", port))]

        with self.assertRaises(MvpError) as raised:
            foundry.verify_models(self.routes(account, deployments), "mvp01-rg", INFRA, ROLES, public)
        message = str(raised.exception)
        for problem in ("public network access Enabled", "no agent network injection",
                        f"must serve {deployments[0]['name']} version", "is Creating, not Succeeded",
                        "resolves to 52.1.2.3"):
            self.assertIn(problem, message)

    def test_the_project_identity_is_granted_the_models_once(self):
        az = FakeAzure([(r"^role assignment list", []), (r"^role assignment create", {"id": "assignment"})])
        self.assertTrue(foundry.grant_project(az, INFRA))
        (created,) = az.called(r"^role assignment create")
        self.assertIn(f"--role {foundry.FOUNDRY_USER} --scope {INFRA['foundryAccountId']}", created)

        az = FakeAzure([(r"^role assignment list", ["/assignments/existing"])])
        self.assertFalse(foundry.grant_project(az, INFRA))
        self.assertEqual(az.called(r"create"), [])

    def test_the_project_endpoint_must_be_a_foundry_project(self):
        endpoint = INFRA["foundryProjectEndpoint"]
        self.assertEqual(foundry.project_endpoint({"foundryProjectEndpoint": endpoint + "/"}), endpoint)
        for wrong in (endpoint.replace("https", "http"), "https://mvp01-ai.example.com/api/projects/passport",
                      "https://mvp01-ai.services.ai.azure.com/projects/passport", endpoint + "?x=1", None):
            with self.subTest(endpoint=wrong), self.assertRaisesRegex(MvpError, "bootstrap --from A1"):
                foundry.project_endpoint({"foundryProjectEndpoint": wrong})


if __name__ == "__main__":
    unittest.main()
