"""Exercise stage B (python -m dbp_mvp deploy) against a fake Azure CLI, a fake SQL driver, fake Blob storage and a
fake Foundry project.

Nothing here signs in to Microsoft Entra ID, reaches Azure SQL, builds an agent or deploys the app: the tests show the
command sequence, the secret handling and the safety checks, not that Azure accepts them.
"""

import base64
import contextlib
import hashlib
import io
import itertools
import json
import re
import socket
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_fabric import App, Capacity, FakeFabric
from test_orchestrator import (CAPACITY, NOT_FOUND, OPERATOR, SUBSCRIPTION, TENANT, Fail, FakeAzure, Raw, Seq,
                               Workspace, make_config, session_routes)

from dbp_mvp import MvpError, cli, data_plane, deploy, foundry, identity, image, progress, smoke, supply
from dbp_mvp.config import MARKER

GRAPH = r"https://graph\.microsoft\.com/v1\.0"
APP_OBJECT = "aaaaaaaa-0000-0000-0000-000000000001"
APP_ID = "aaaaaaaa-0000-0000-0000-000000000002"
SP = "aaaaaaaa-0000-0000-0000-000000000003"
ROLE_IDS = {"Passport.Operator": "r-operator", "Passport.Reviewer": "r-reviewer", "Passport.Connector": "r-connector",
            "Passport.Compliance": "r-compliance", "Passport.Admin": "r-admin", "Passport.OEM": "r-oem",
            "Passport.Recycler": "r-recycler", "Passport.Authority": "r-authority"}
KEY = "key-new"
SECRET = "Sup3r~SignInSecretValue.0123456789"
NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
RG = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/mvp01-rg/providers"
VNET = f"{RG}/Microsoft.Network/virtualNetworks/mvp01-vnet"
APP_URL = "https://mvp01-passport.calmsea-1234.swedencentral.azurecontainerapps.io"
APP_STATUS = {"mode": "Single", "revision": "mvp01-passport--new", "readyRevision": "mvp01-passport--new",
              "state": "Succeeded", "containers": [{"name": "passport",
                                                     "image": "mvp01acr.azurecr.io/passport-app:20261112-080000"}]}
APP_SERVING = {"weight": 100, "images": [APP_STATUS["containers"][0]["image"]]}
INSIGHTS = "dddddddd-0000-0000-0000-000000000001"
PROJECT_PRINCIPAL = "eeeeeeee-0000-0000-0000-000000000001"
AGENT_PRINCIPALS = {"supplier": "bbbbbbbb-0000-0000-0000-000000000001",
                    "lifecycle": "bbbbbbbb-0000-0000-0000-000000000002"}
AGENT_CLIENTS = {"supplier": "cccccccc-0000-0000-0000-000000000001",
                 "lifecycle": "cccccccc-0000-0000-0000-000000000002"}
TOKEN = "eyJ0eXAiOiJKV1QifQ.operator-token.c2lnbmF0dXJl"
INFRA = {
    "resourceGroupName": "mvp01-rg", "agentsSubnetId": f"{VNET}/subnets/agents",
    "appIdentityClientId": "11111111-2222-3333-4444-555555555555", "appIdentityName": "mvp01-app-id",
    "appParameters": {"prefix": "mvp01", "environmentName": "mvp01-env", "registryName": "mvp01acr"},
    "appStorageName": "mvp01app", "appStorageUrl": "https://mvp01app.blob.core.windows.net/",
    "applicationInsightsAppId": INSIGHTS,
    "databaseName": "passport", "digestStorageName": "mvp01digest",
    "digestStorageUrl": "https://mvp01digest.blob.core.windows.net/",
    "environmentDomain": "calmsea-1234.swedencentral.azurecontainerapps.io", "environmentName": "mvp01-env",
    "foundryAccountId": f"{RG}/Microsoft.CognitiveServices/accounts/mvp01-ai", "foundryAccountName": "mvp01-ai",
    "foundryProjectEndpoint": "https://mvp01-ai.services.ai.azure.com/api/projects/passport",
    "foundryProjectPrincipalId": PROJECT_PRINCIPAL,
    "logsName": "mvp01-logs", "registryLoginServer": "mvp01acr.azurecr.io", "registryName": "mvp01acr",
    "sqlServerFqdn": "mvp01-sql.database.windows.net", "sqlServerName": "mvp01-sql",
    "workbookIds": [{"persona": persona, "id": f"{RG}/Microsoft.Insights/workbooks/{persona}-id"}
                    for persona in ("executive", "compliance", "agents", "operations")],
    "fabricCapacityId": CAPACITY,
}
ROLES = foundry.contracts().ROLES
FOUNDRY_ACCOUNT = {"properties": {
    "provisioningState": "Succeeded", "publicNetworkAccess": "Disabled",
    "networkInjections": [{"scenario": "agent", "subnetArmId": INFRA["agentsSubnetId"],
                           "useMicrosoftManagedNetwork": False}]}}
MODEL_DEPLOYMENTS = [{"name": model, "properties": {"model": {"format": "OpenAI", "name": model, "version": version},
                                                    "provisioningState": "Succeeded"}}
                     for model, version in sorted({(spec["model"], spec["version"]) for spec in ROLES.values()})]
TELEMETRY_FOUND = {"tables": [{"name": "PrimaryResult", "rows": [["agent.run"]]}]}
TELEMETRY_EMPTY = {"tables": [{"name": "PrimaryResult", "rows": []}]}
TABLES = (["name", "ledger_type_desc"], [("PassportVersions", "APPEND_ONLY_LEDGER_TABLE"),
                                         ("Passports", "UPDATABLE_LEDGER_TABLE")])


def setUpModule():
    progress.print = lambda *args, **kwargs: None


def tearDownModule():
    del progress.print


# ---- fake SQL driver and Blob storage -------------------------------------------------------------------------

class DriverError(Exception):
    pass


class Cursor:
    def __init__(self, database):
        self.database, self.sets, self.rows, self.description = database, [], [], None

    def execute(self, sql, *parameters):
        self.database.executed.append(sql)
        self.sets = list(self.database.answer(sql, parameters))
        self._load()

    def _load(self):
        if not self.sets:
            self.description, self.rows = None, []
            return False
        current = self.sets.pop(0)
        if isinstance(current, Exception):
            raise current
        if current is None:
            self.description, self.rows = None, []
        else:
            columns, rows = current
            self.description, self.rows = [(column,) for column in columns], list(rows)
        return True

    def nextset(self):
        return self._load()

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class Database:
    """Answers the schema scripts, the table summary, digest generation and ledger verification."""

    def __init__(self):
        self.executed, self.block, self.verified, self.opened, self.closed = [], 6, None, [], 0
        self.verify_error, self.created = None, datetime(2026, 10, 23, 8, 0, 0)

    def connect(self, outputs, sleep, out, az=None):
        self.opened.append(len(az.calls) if az else 0)
        database = self

        class Connection:
            def cursor(self):
                return Cursor(database)

            def close(self):
                database.closed += 1

        return Connection()

    def answer(self, sql, parameters):
        if sql == data_plane.GENERATE_SQL:
            self.block += 1
            digest = {"database_name": "passport", "block_id": self.block, "hash": f"0x{self.block:064x}"}
            return [None, (["latest_digest"], [(json.dumps(digest),)])]
        if sql == data_plane.app_module("ledger").VERIFY_SQL:
            if self.verify_error:
                return [self.verify_error]
            covered = max(item["block_id"] for item in json.loads(parameters[0]))
            return [None, (["last_verified_block_id"], [(self.verified if self.verified is not None else covered,)])]
        if sql == data_plane.TABLES_SQL:
            return [TABLES]
        if sql == data_plane.app_module("ledger").INCARNATION_SQL:
            return [(["create_date"], [(self.created,)])]
        return [None]


class Forbidden(Exception):
    status_code = 403


class Blobs:
    def __init__(self, forbidden=0):
        self.items, self.forbidden, self.uploads = {}, forbidden, []

    def __call__(self, account_url, name):
        blobs = self

        class Container:
            def __enter__(self):
                if blobs.forbidden:
                    blobs.forbidden -= 1
                    raise Forbidden("This request is not authorized. AuthorizationPermissionMismatch")
                return self

            def __exit__(self, *exc):
                return False

            def upload_blob(self, blob, data, overwrite=True):
                assert overwrite is False and blob not in blobs.items
                blobs.items[blob] = data
                blobs.uploads.append((account_url, name, blob))

            def list_blobs(self, name_starts_with=""):
                return [type("Blob", (), {"name": key}) for key in blobs.items if key.startswith(name_starts_with)]

            def download_blob(self, blob):
                return type("Download", (), {"readall": lambda _: blobs.items[blob]})()

        return Container()


def ready(_url):
    return 200, json.dumps({"status": "ready", "store": "sql", "blobs": "azure"})


def private_address(host, port, proto=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("10.20.3.4", port))]


# ---- fake Foundry project and passport app ----------------------------------------------------------------------

class Model:
    """Stands in for an azure.ai.projects.models class: keeps its keyword arguments as attributes."""
    kind = None

    def __init__(self, **values):
        self.__dict__.update(values)


class HostedAgentDefinition(Model):
    kind = "hosted"


class PromptAgentDefinition(Model):
    kind = "prompt"


SDK = SimpleNamespace(HostedAgentDefinition=HostedAgentDefinition, PromptAgentDefinition=PromptAgentDefinition,
                      ProtocolVersionRecord=Model, CodeConfiguration=Model, FunctionTool=Model)


class Missing(Exception):
    status_code = 404


class Foundry:
    """project.agents of a Foundry project. An uploaded hosted version is creating until it is polled once."""

    def __init__(self):
        self.versions, self.uploads, self.created, self.opened, self.polled = {}, [], [], [], []
        self.after_build = "active"
        self.build_error = None

    def __call__(self, endpoint):
        self.opened.append(endpoint)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def agents(self):
        return self

    def _add(self, name, definition, metadata, status):
        versions = self.versions.setdefault(name, [])
        role = name.removeprefix("dbp-")
        found = (SimpleNamespace(principal_id=AGENT_PRINCIPALS[role], client_id=AGENT_CLIENTS[role])
                 if definition.kind == "hosted" else None)
        versions.append(SimpleNamespace(version=str(len(versions) + 1), status=status, metadata=dict(metadata),
                                        definition=definition, instance_identity=found))
        return versions[-1]

    def get(self, agent_name):
        if agent_name not in self.versions:
            raise Missing(agent_name)
        latest = self.versions[agent_name][-1]
        return SimpleNamespace(versions=SimpleNamespace(latest=latest), instance_identity=latest.instance_identity)

    def get_version(self, agent_name, agent_version):
        self.polled.append(agent_name)
        version = self.versions[agent_name][int(agent_version) - 1]
        version.status = self.after_build
        version.error = self.build_error
        return version

    def create_version_from_code(self, name, definition, code, code_zip_sha256, description, metadata):
        assert code.name == "agent.zip" and hashlib.sha256(code.getvalue()).hexdigest() == code_zip_sha256
        self.uploads.append(name)
        return self._add(name, definition, metadata, "creating")

    def create_version(self, agent_name, definition, metadata, description):
        self.created.append(agent_name)
        return self._add(agent_name, definition, metadata, None)


class Supplier:
    """POST /agents/supplier/run of the passport app; the answers come in turn and the last one repeats."""

    def __init__(self):
        self.answers, self.calls = [(200, {"outcome": {"status": "deferred"}})], []

    def __call__(self, url, body, token):
        self.calls.append((url, body, token))
        status, answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return status, json.dumps(answer)


# ---- fake Azure ------------------------------------------------------------------------------------------------

def graph_routes(apps=(), assigned=(), credentials=(), owners=()):
    app = {"id": APP_OBJECT, "appId": APP_ID, "tags": [MARKER], "appRoles": [], "api": {},
           "passwordCredentials": list(credentials)}
    return [
        (rf"--method get --url {GRAPH}/applications --url-parameters", {"value": list(apps)}),
        (rf"--method post --url {GRAPH}/applications --headers", {"id": APP_OBJECT, "appId": APP_ID}),
        (rf"--method get --url {GRAPH}/applications/{APP_OBJECT}\b", app),
        (rf"--method patch --url {GRAPH}/applications/", None),
        (rf"--method get --url {GRAPH}/(applications|servicePrincipals)/[^/ ]+/owners ",
         {"value": [{"id": owner} for owner in owners]}),
        (rf"--method post --url {GRAPH}/(applications|servicePrincipals)/[^/ ]+/owners/\$ref ", None),
        (rf"--method get --url {GRAPH}/servicePrincipals --url-parameters", {"value": []}),
        (rf"--method post --url {GRAPH}/servicePrincipals --headers", {"id": SP}),
        (rf"--method get --url {GRAPH}/servicePrincipals/{SP} ",
         {"appRoles": [{"value": value, "id": role} for value, role in ROLE_IDS.items()]}),
        (rf"--method get --url {GRAPH}/servicePrincipals/{SP}/appRoleAssignedTo",
         {"value": [{"id": f"a-{ROLE_IDS[value]}", "principalId": OPERATOR, "appRoleId": ROLE_IDS[value]}
                    for value in assigned]}),
        (rf"--method delete --url {GRAPH}/servicePrincipals/{SP}/appRoleAssignedTo/", None),
        (rf"--method get --url {GRAPH}/me ", {"id": OPERATOR, "userPrincipalName": "operator"}),
        (rf"--method post --url {GRAPH}/servicePrincipals/{SP}/appRoleAssignedTo", {}),
        (r"/addPassword", {"keyId": KEY, "secretText": SECRET}),
        (r"/removePassword", None),
    ]


class AppRoles:
    """The registration's app roles as Graph returns them after the last PATCH."""

    def __init__(self):
        self.roles = []

    def read(self, args):
        return {"appRoles": self.roles}

    def patch(self, args):
        body = json.loads(Path(args[args.index("--body") + 1][1:]).read_text(encoding="utf-8"))
        self.roles = body.get("appRoles", self.roles)


def agent_client(args):
    principal = next(arg for arg in args if "/servicePrincipals/" in arg).rsplit("/", 1)[1]
    clients = {AGENT_PRINCIPALS[role]: AGENT_CLIENTS[role] for role in AGENT_PRINCIPALS}
    return {"appId": clients[principal].upper()}


def agent_routes():
    roles = AppRoles()
    principal = r"bbbbbbbb-0000-0000-0000-00000000000\d"
    return [
        (r"^cognitiveservices account show", FOUNDRY_ACCOUNT),
        (r"^cognitiveservices account deployment list", MODEL_DEPLOYMENTS),
        (r"^role assignment list", []),
        (r"^role assignment create", {"id": "assignment"}),
        (rf"--method get --url {GRAPH}/applications/{APP_OBJECT} --url-parameters \$select=appRoles", roles.read),
        (rf"--method patch --url {GRAPH}/applications/{APP_OBJECT}", roles.patch),
        (rf"--method get --url {GRAPH}/servicePrincipals/{principal}/appRoleAssignments", {"value": []}),
        (rf"--method post --url {GRAPH}/servicePrincipals/{principal}/appRoleAssignments", {"id": "granted"}),
        (rf"--method get --url {GRAPH}/servicePrincipals/{principal} --url-parameters", agent_client),
        (r"^account get-access-token", Raw(TOKEN)),
        (r"--url https://api\.applicationinsights\.io/", TELEMETRY_FOUND),
    ]


def cloud_routes(app_present=False):
    return session_routes() + graph_routes() + agent_routes() + [
        (r"^deployment sub show", {"state": "Succeeded",
                                   "outputs": {key: {"value": value} for key, value in INFRA.items()}}),
        (r"^acr show", {"access": "Enabled", "sku": "Standard", "admin": False}),
        (r"^acr repository list", ["passport-app"]),
        (r"^acr repository delete", None),
        (r"^acr build", None),
        (r"^containerapp show .*--query " + re.escape(deploy.APP_PRICES_QUERY),
         None if app_present else NOT_FOUND),
        (r"^containerapp show .*--query " + re.escape(deploy.APP_STATUS_QUERY), APP_STATUS),
        (r"^containerapp revision show .*--query " + re.escape(deploy.APP_REVISION_QUERY), APP_SERVING),
        (r"Microsoft\.App/containerApps", f"{RG}/Microsoft.App/containerApps/mvp01-passport" if app_present
         else NOT_FOUND),
        (r"^deployment group create", {"appName": {"value": "mvp01-passport"},
                                       "appFqdn": {"value": APP_URL[8:]}, "appUrl": {"value": APP_URL}}),
        (r"^resource show .*--query properties\.publicNetworkAccess", "Disabled"),
        (r"^monitor log-analytics workspace show", {"ingestion": "Enabled", "query": "Enabled"}),
        (r"Microsoft\.App/managedEnvironments", {"access": "Enabled", "internal": False,
                                                 "subnet": f"{VNET}/subnets/apps"}),
        (r"^network private-endpoint list", [{"name": "mvp01-sql-pe", "states": ["Approved"]}]),
    ] + Capacity().power_routes()


class Stage(Workspace):
    """Stage B against the fakes. B5 runs the Fabric pipeline in FakeFabric; B6 imports into the app's own service."""

    def setUp(self):
        super().setUp()
        folder = self.state.path.parent
        self.config = make_config()
        self.database, self.blobs, self.now = Database(), Blobs(), NOW
        self.foundry, self.supplier = Foundry(), Supplier()
        self.fabric = FakeFabric(folder / "onelake", token=TOKEN)
        self.app = App(folder / "app-storage", token=TOKEN, output=folder / "output")
        self.sleeps = []

    def runtime(self, az, **overrides):
        values = {"sleep": self.sleeps.append, "clock": lambda: 0, "now": lambda: self.now,
                  "resolve": private_address, "http_get": ready, "container": self.container,
                  "connect": lambda outputs, sleep, out: self.database.connect(outputs, sleep, out, az),
                  "http_post": self.post, "http_post_multipart": self.app.post_multipart,
                  "http_get_auth": self.app.get, "fabric_send": self.fabric,
                  "output": self.state.path.parent / "output", "project": self.foundry, "models": lambda: SDK}
        return deploy.Runtime(**{**values, **overrides})

    def container(self, account_url, name):
        return contextlib.nullcontext(self.app) if name == supply.INTAKE else self.blobs(account_url, name)

    def post(self, url, body, token):
        return (self.supplier if url.endswith("/agents/supplier/run") else self.app.post)(url, body, token)

    def deploy(self, az, **options):
        runtime = options.pop("runtime", None) or self.runtime(az)
        deploy.run(self.config, az, self.state, runtime=runtime, out=self.out, **options)

    def assert_secret_kept_out(self, az):
        state_text = self.state.path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, state_text)
        self.assertNotIn(SECRET, self.printed)
        self.assertFalse([call for call in az.calls if SECRET in call])
        holders = [call for call, _, content in az.files if SECRET in content]
        self.assertTrue(all(call.startswith("deployment group create") for call in holders))

    @staticmethod
    def app_deployments(az):
        return [json.loads(content)["parameters"] for call, _, content in az.files
                if call.startswith("deployment group create")]


# ---- the whole stage --------------------------------------------------------------------------------------------

class DeployTests(Stage):
    def test_clean_reset_requires_typed_confirmation_before_any_deletion(self):
        self.deploy(FakeAzure(cloud_routes()))
        az = FakeAzure(cloud_routes(app_present=True) +
                       [(r"^group show", {"tags": {"project": "battery-passport"}})])

        with self.assertRaisesRegex(MvpError, "Cancelled; no data was deleted"):
            deploy.data_clean(self.config, az, self.state, runtime=self.runtime(az), confirm=lambda _: "no",
                              out=self.out)

        self.assertEqual(az.called(r"^sql db delete|^resource delete|--method put|revision deactivate"), [])
        self.assertFalse((self.state.path.parent / "output" / "data-clean.json").exists())

    def test_task_hub_reset_waits_for_delete_and_recreates_with_template_api(self):
        resource_id = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/mvp01-rg/"
                       "providers/Microsoft.DurableTask/schedulers/mvp01-dts/taskHubs/mvp01-passport-hub")

        class Azure:
            exists = True
            calls = []

            def find(self, *args):
                self.calls.append(("find", *args))
                return {"properties": {"provisioningState": "Succeeded"}} if self.exists else None

            def __call__(self, *args):
                self.calls.append(("command", *args))
                self.exists = False

            def rest(self, method, url, body=None):
                self.calls.append(("rest", method, url, body))
                self.exists = True

        az = Azure()
        deploy._reset_task_hub(self.config, az, self.runtime(None), self.out)

        self.assertEqual(az.calls[1], ("command", "resource", "delete", "--ids", resource_id))
        put = next(call for call in az.calls if call[0] == "rest")
        self.assertEqual(put[1:], ("put", f"https://management.azure.com{resource_id}"
                                         f"?api-version={deploy.DURABLE_TASK_API}", {"properties": {}}))
        self.assertEqual(az.calls[-1][0:2], ("find", "resource"))

    class VersionedBlobs:
        """Blob versioning as Azure applies it: deleting a blob keeps its current version as a previous one, and
        deleting a previous version is refused (403 under a version error code) until the Owner role applies."""

        def __init__(self, refusals=0):
            self.versions = {name: {f"{name}-1.json": [("v1", False), ("v2", True)]}
                             for name in ("evidence", "intake", "settings")}
            self.versions["passports"] = {}
            self.refusals = refusals

        def open(self, _url, name):
            versions = self.versions[name]

            def delete_blob(blob, delete_snapshots=None, version_id=None):
                if version_id is None:
                    assert delete_snapshots == "include"
                    versions[blob] = [(version, False) for version, _ in versions[blob]]
                    return
                if self.refusals:
                    from azure.core.exceptions import HttpResponseError
                    self.refusals -= 1
                    error = HttpResponseError(message="The specified operation is not allowed on version.")
                    error.status_code, error.error_code = 403, "OperationNotAllowedOnAutomaticSnapshot"
                    raise error
                versions[blob] = [item for item in versions[blob] if item[0] != version_id]

            def list_blobs(include):
                assert include == ["versions"]
                return [SimpleNamespace(name=blob, version_id=version, is_current_version=current or None)
                        for blob, items in versions.items() for version, current in items]

            return contextlib.nullcontext(SimpleNamespace(
                list_blob_names=lambda: [blob for blob, items in versions.items() if any(c for _, c in items)],
                list_blobs=list_blobs, delete_blob=delete_blob))

    def clear_blobs(self, az, blobs, sleeps):
        deploy._clear_app_blobs(self.config, az, INFRA, OPERATOR,
                                SimpleNamespace(container=blobs.open, sleep=sleeps.append), self.out)

    def test_blob_clean_deletes_versions_under_a_temporary_owner_role_and_keeps_settings(self):
        blobs, sleeps = self.VersionedBlobs(refusals=1), []
        az = FakeAzure([(r"^role assignment list", []), (r"^role assignment create", "grant-id"),
                        (r"^role assignment delete", None)])

        self.clear_blobs(az, blobs, sleeps)

        for name in ("evidence", "passports", "intake"):
            self.assertFalse([item for items in blobs.versions[name].values() for item in items])
        self.assertEqual(blobs.versions["settings"]["settings-1.json"], [("v1", False), ("v2", True)])
        self.assertEqual(sleeps, [30])
        (created,) = az.called(r"^role assignment create")
        self.assertIn(f"--assignee-object-id {OPERATOR} --assignee-principal-type User "
                      "--role Storage Blob Data Owner --scope "
                      f"/subscriptions/{self.config.subscription_id}/resourceGroups/mvp01-rg/providers/"
                      "Microsoft.Storage/storageAccounts/mvp01app", created)
        self.assertEqual(az.called(r"^role assignment delete"), ["role assignment delete --ids grant-id"])

        calls = len(az.calls)
        self.clear_blobs(az, blobs, sleeps)
        self.assertEqual(len(az.calls), calls)  # nothing versioned is left, so no role is granted again

    def test_a_grant_left_by_an_interrupted_clean_is_removed_next_time(self):
        az = FakeAzure([(r"^role assignment list", []), (r"^role assignment create", "grant-id"),
                        (r"^role assignment delete", Fail("ERROR: network"))])
        self.clear_blobs(az, self.VersionedBlobs(), [])
        self.assertIn("az role assignment delete --ids grant-id", self.printed)
        name = re.search(r"--name (\S+)", az.called(r"^role assignment create")[0]).group(1)

        az = FakeAzure([(r"^role assignment list", [{"id": "inherited-id", "name": "inherited"},
                                                   {"id": "grant-id", "name": name}]),
                        (r"^role assignment delete", None)])
        self.clear_blobs(az, self.VersionedBlobs(), [])

        self.assertEqual(az.called(r"^role assignment create"), [])
        self.assertEqual(az.called(r"^role assignment delete"), ["role assignment delete --ids grant-id"])

    def test_clean_reset_removes_only_generated_local_data(self):
        output = self.state.path.parent / "output"
        output.mkdir()
        for name in ("series", "runs", "exports", "evaluation", "credential_negatives", "keys"):
            (output / name).mkdir()
            (output / name / "keep.txt").write_text("data", encoding="utf-8")
        for name in ("state.json", "data-clean.json"):
            (output / name).write_text("state", encoding="utf-8")

        deploy._clear_local_generated_data(output)

        self.assertFalse(any((output / name).exists()
                             for name in ("series", "runs", "exports", "evaluation", "credential_negatives")))
        self.assertTrue((output / "keys" / "keep.txt").exists())
        self.assertTrue((output / "state.json").exists())
        self.assertTrue((output / "data-clean.json").exists())

    def test_clean_reset_runs_ordered_purge_then_redeploys_a_fresh_build(self):
        self.deploy(FakeAzure(cloud_routes()))
        az = FakeAzure(cloud_routes(app_present=True) +
                       [(r"^group show", {"tags": {"project": "battery-passport"}})])
        calls = []

        def record(name):
            return lambda *args, **kwargs: calls.append((name, kwargs))

        with (mock.patch.object(deploy, "_deactivate_app", record("deactivate")),
              mock.patch.object(deploy, "_reset_task_hub", record("task-hub")),
              mock.patch.object(deploy, "_clear_app_blobs", record("blobs")),
              mock.patch.object(supply, "clear_fabric_data", record("fabric")),
              mock.patch.object(deploy, "_recreate_database", record("database")),
              mock.patch.object(deploy, "_clear_local_generated_data", record("local")),
              mock.patch.object(deploy, "run", record("deploy")),
              mock.patch.object(deploy, "report_publish", record("report"))):
            deploy.data_clean(self.config, az, self.state, yes=True, runtime=self.runtime(az), out=self.out)

        self.assertEqual([name for name, _ in calls],
                         ["deactivate", "task-hub", "blobs", "fabric", "database", "local", "deploy", "report"])
        self.assertTrue(dict(calls)["deploy"]["new_build"])
        self.assertEqual(dict(calls)["deploy"]["start"], "B2")
        self.assertTrue(dict(calls)["deploy"]["skip_supplier_check"])
        self.assertFalse((self.state.path.parent / "output" / "data-clean.json").exists())
        self.assertIn("Next, start the story: python -m dbp_mvp run story", self.printed)

    def test_first_run_prepares_the_database_before_the_app(self):
        az = FakeAzure(cloud_routes())
        self.deploy(az)

        self.assertEqual(set(self.state.data["phases"]), set(deploy.ORDER))
        self.assertLess(az.index(r"--method post --url \S+/applications --headers"), self.database.opened[0])
        self.assertLessEqual(self.database.opened[0], az.index(r"^acr build"))
        self.assertLess(az.index(r"^acr build"), az.index(r"^deployment group create"))
        self.assertEqual(len(az.called(r"/appRoleAssignedTo --headers")), 4)  # not admin
        self.assertEqual(self.database.closed, len(self.database.opened))
        self.assertEqual(self.blobs.uploads[0][2].split("/")[:3], ["manual", "mvp01-sql", "passport"])

        parameters = json.loads(az.file(r"^deployment group create"))["parameters"]
        self.assertEqual(parameters["appClientSecret"]["value"], SECRET)
        self.assertEqual(parameters["appClientId"]["value"], APP_ID)
        self.assertRegex(parameters["image"]["value"], r"^mvp01acr\.azurecr\.io/passport-app:20261112-080000$")
        self.assertFalse(parameters["agentMode"]["value"])
        self.assertEqual(parameters["workbookLinks"]["value"]["executive"],
                         f"https://portal.azure.com/#@{TENANT}/resource{RG}/Microsoft.Insights/workbooks/executive-id")
        self.assertEqual(self.state.outputs("B3")["sign_in_expires"], "2026-12-12")
        self.assertEqual(self.state.outputs("B1")["roles"],
                         ["Passport.Compliance", "Passport.Connector", "Passport.Operator", "Passport.Reviewer"])
        registry = json.loads(base64.b64decode(parameters["partyRegistryB64"]["value"]))
        self.assertEqual(registry["schema"], "dbp-party-registry-1")
        self.assertNotIn("PRIVATE", json.dumps(registry).upper())
        self.assert_secret_kept_out(az)
        self.assertIn(APP_URL, self.printed)
        self.assertIn("Users and groups", self.printed)
        self.assertIn("python -m dbp_mvp vm stop", self.printed)

        supplied, seeded = self.state.outputs("B5"), self.state.outputs("B6")
        self.assertEqual((supplied["package_source"], seeded["build_id"]), ("fabric", supplied["build_id"]))
        self.assertEqual(re.findall(r"/(resume|suspend)\?", "\n".join(az.calls)),
                         ["resume", "suspend", "resume", "suspend"],
                         "B5 supply and B6 aftersales each pause Fabric; B8 must not suspend it again.")
        self.assertIsNotNone(seeded["aftersales"])
        self.assertTrue(self.app.uploaded and self.app.service.report_rows())
        self.assertIn("came from built in Fabric, byte-identical to the local build", self.printed)
        self.assertIn(f"Imported package {seeded['build_id']}: Fabric built it and it matches the local build.",
                      self.printed)

    def test_data_reset_recreates_the_database_only_after_confirmation(self):
        self.deploy(FakeAzure(cloud_routes()))
        database = {"sku": {"tier": "GeneralPurpose", "family": "Gen5", "capacity": 2}, "useFreeLimit": True,
                    "freeLimitExhaustionBehavior": "AutoPause", "requestedBackupStorageRedundancy": "Zone",
                    "tags": {"purpose": "mvp"}}
        routes = cloud_routes(app_present=True) + [(r"^sql db show", database), (r"^sql db (delete|create)", None)]
        az = FakeAzure(routes)
        with self.assertRaisesRegex(MvpError, "nothing was deleted"):
            deploy.data_reset(self.config, az, self.state, runtime=self.runtime(az), confirm=lambda _: "no",
                              out=self.out)
        self.assertEqual(az.called(r"^sql db (delete|create)"), [])
        self.assertIn("B2", self.state.data["phases"])

        opened = len(self.database.opened)
        self.database.created = datetime(2026, 11, 13, 9, 0, 0)
        az = FakeAzure(routes)
        result = deploy.data_reset(self.config, az, self.state, yes=True, runtime=self.runtime(az), out=self.out)
        self.assertLess(az.index(r"^sql db delete .*--yes"), az.index(r"^sql db create"))
        create = az.called(r"^sql db create")[0]
        for option in ("--edition GeneralPurpose", "--family Gen5", "--capacity 2", "--compute-model Serverless",
                       "--auto-pause-delay 60", "--min-capacity 0.5", "--backup-storage-redundancy Zone",
                       "--tags purpose=mvp"):
            self.assertIn(option, create)
        self.assertNotIn("--use-free-limit", create)
        self.assertNotIn("--free-limit-exhaustion-behavior", create)
        self.assertEqual(len(self.database.opened), opened + 1)
        self.assertTrue(any("GRANT SELECT, INSERT ON OBJECT::dbp.PassportVersions" in sql
                            for sql in self.database.executed))
        self.assertTrue(any(upload[2].startswith("manual/mvp01-sql/passport/20261113T090000Z/")
                            for upload in self.blobs.uploads))
        self.assertEqual(result["seed"]["build_id"], self.state.outputs("B6")["build_id"])
        self.assertNotIn("B7", self.state.data["phases"])

    def test_a_package_built_locally_after_a_fabric_failure_is_tried_in_fabric_again(self):
        self.fabric.broken.add("silver_normalise")
        self.deploy(FakeAzure(cloud_routes()))
        self.assertEqual(self.state.outputs("B5")["package_source"], "local")
        self.assertIn("came from the local build (the Fabric run failed", self.printed)
        self.assertRegex(self.printed, r"Imported package \S+ from the local build \(the Fabric run failed")

        self.fabric.broken.clear()
        self.lines.clear()
        self.deploy(FakeAzure(cloud_routes(app_present=True)))
        self.assertNotIn("B5 synthetic data: unchanged, skipped.", self.printed)
        self.assertIn("B6 seed data: unchanged, skipped.", self.printed)
        self.assertEqual(self.state.outputs("B5")["package_source"], "fabric")
        self.assertEqual([flow for flow, _ in self.fabric.runs], ["supply_flow", "aftersales_flow", "supply_flow"])

    def test_rerun_skips_finished_phases_but_repeats_the_checks(self):
        self.deploy(FakeAzure(cloud_routes()))
        az = FakeAzure(cloud_routes(app_present=True))
        self.lines.clear()
        self.deploy(az)

        for phase in ("B1 app registration", "B2 database", "B3 app", "B4 agents"):
            self.assertIn(f"{phase}: unchanged, skipped.", self.printed)
        self.assertEqual(az.called(r"addPassword|^acr build|^deployment group create|--method (post|patch)"), [])
        self.assertTrue(az.called(r"^network private-endpoint list"))
        self.assertEqual(len(self.database.opened), 3)  # B2 and B7 of the first run, B7 of the second
        self.assertEqual(len(self.foundry.opened), 1)
        tasks = [body["task_id"] for _, body, _ in self.supplier.calls]
        self.assertEqual((len(tasks), len(set(tasks))), (2, 1))  # the app answers a repeated task from its record
        self.assertEqual(az.called(r"applicationinsights"), [])

    def test_an_expiring_sign_in_secret_is_renewed_without_a_rebuild(self):
        self.deploy(FakeAzure(cloud_routes()))
        image_before = self.state.outputs("B3")["image"]
        self.now = NOW + timedelta(days=25)
        az = FakeAzure(cloud_routes(app_present=True))
        self.deploy(az)

        self.assertIn("renewing it", self.printed)
        self.assertEqual(az.called(r"^acr build"), [])
        self.assertEqual(len(az.called(r"addPassword")), 1)
        self.assertEqual(json.loads(az.file(r"^deployment group create"))["parameters"]["image"]["value"],
                         image_before)
        self.assertEqual(self.state.outputs("B3")["sign_in_expires"], "2027-01-06")

    def test_a_template_only_change_reapplies_the_passport_app(self):
        template = self.state.path.parent / "app.bicep"
        template.write_text("param maxReplicas int = 1", encoding="utf-8")
        with mock.patch.object(deploy, "TEMPLATE", template):
            self.deploy(FakeAzure(cloud_routes()))
            template.write_text("param maxReplicas int = 3", encoding="utf-8")
            az = FakeAzure(cloud_routes(app_present=True))
            self.deploy(az)
        [parameters] = self.app_deployments(az)
        self.assertTrue(parameters["agentMode"]["value"])

    def test_token_prices_reach_the_app_only_when_configured(self):
        unpriced = FakeAzure(cloud_routes())
        self.deploy(unpriced)
        self.assertNotIn("pricesB64", self.app_deployments(unpriced)[0])
        priced = FakeAzure(cloud_routes(app_present=True))
        self.config = make_config(prices={"currency": "USD", "models": {
            "gpt-5.4": {"input_per_million": 1.25, "output_per_million": 10}}})
        self.lines.clear()
        self.deploy(priced)

        self.assertNotIn("B3 app: unchanged, skipped.", self.printed)
        [parameters] = self.app_deployments(priced)
        self.assertEqual(json.loads(base64.b64decode(parameters["pricesB64"]["value"])), self.config.prices)

    def test_later_deployments_preserve_applied_prices_over_stale_or_missing_vm_prices(self):
        applied = {"currency": "USD", "source": "updated list", "models": {
            "gpt-6.1-sol": {"input_per_million": 2, "output_per_million": 3}}}
        encoded = deploy.prices_parameter(make_config(prices=applied))["pricesB64"]
        self.deploy(FakeAzure(cloud_routes()))
        for stale in ({**applied, "source": "old VM copy"}, None):
            with self.subTest(stale=stale):
                self.config = make_config(prices=stale)
                az = FakeAzure(cloud_routes(app_present=True)).on(
                    r"^containerapp show .*--query " + re.escape(deploy.APP_PRICES_QUERY),
                    [{"prices": [{"name": "DBP_PRICES_B64", "value": encoded}]}])
                self.deploy(az, start="B3")
                parameters = self.app_deployments(az)
                self.assertTrue(parameters)
                for item in parameters:
                    self.assertEqual(json.loads(base64.b64decode(item["pricesB64"]["value"])), applied)

    def test_from_b3_rebuilds_and_removes_older_secrets(self):
        self.deploy(FakeAzure(cloud_routes()))
        az = FakeAzure(cloud_routes(app_present=True) + graph_routes(credentials=[
            {"keyId": "key-old", "displayName": identity.SECRET_NAME},
            {"keyId": KEY, "displayName": identity.SECRET_NAME},
            {"keyId": "key-other", "displayName": "someone else's"}]))
        self.deploy(az, start="B3")

        self.assertIn("B2 database: unchanged, skipped.", self.printed)
        self.assertTrue(az.called(r"^acr build"))
        self.assertEqual(len(az.called(r"/removePassword")), 1)
        self.assertIn('"key-old"', az.file(r"/removePassword"))

    def test_a_failed_app_deployment_removes_the_new_secret(self):
        az = FakeAzure(cloud_routes()).on(r"^deployment group create", Fail("ERROR: InvalidTemplateDeployment"))
        with self.assertRaises(MvpError):
            self.deploy(az)

        self.assertIn(f'"{KEY}"', az.file(r"/removePassword"))
        self.assertFalse(self.state.done("B3"))
        self.assert_secret_kept_out(az)

    def test_missing_infrastructure_points_to_the_bootstrap(self):
        az = FakeAzure(cloud_routes()).on(r"^deployment sub show", NOT_FOUND)
        with self.assertRaisesRegex(MvpError, "bootstrap on your laptop"):
            self.deploy(az)
        self.assertEqual(az.called(r"^rest"), [])

    def test_refused_entra_changes_name_the_directory_role_and_the_phase_to_rerun(self):
        denied = Fail('ERROR: Forbidden({"error":{"code":"Authorization_RequestDenied",'
                      '"message":"Insufficient privileges to complete the operation."}})')
        az = FakeAzure(cloud_routes()).on(r"/owners/\$ref", denied)
        with self.assertRaisesRegex(MvpError, r"Cloud Application Administrator.*az login.*--from B1"):
            self.deploy(az)
        self.assertFalse(self.state.done("B1"))

        self.deploy(FakeAzure(cloud_routes()))
        az = FakeAzure(cloud_routes()).on(rf"--method patch --url {GRAPH}/applications/{APP_OBJECT} ", denied)
        with self.assertRaisesRegex(MvpError, r"Passport\.Agent\.Supplier.*az login.*--from B4"):
            self.deploy(az, start="B4")
        self.assertFalse(self.state.done("B4"))

    def test_skip_build_needs_an_earlier_image(self):
        az = FakeAzure(cloud_routes())
        with self.assertRaisesRegex(MvpError, "none is recorded"):
            self.deploy(az, skip_build=True)
        self.assertEqual(az.called(r"^rest|^deployment|^acr"), [])
        self.assertEqual(self.database.opened, [])

    def test_from_b3_with_skip_build_redeploys_the_recorded_image(self):
        self.deploy(FakeAzure(cloud_routes()))
        image_before = self.state.outputs("B3")["image"]
        az = FakeAzure(cloud_routes(app_present=True))
        self.deploy(az, start="B3", skip_build=True)

        self.assertEqual(az.called(r"^acr build"), [])
        self.assertEqual(json.loads(az.file(r"^deployment group create"))["parameters"]["image"]["value"],
                         image_before)
        self.assertEqual(self.state.outputs("B3")["image"], image_before)

    def test_failed_checks_stop_before_the_summary(self):
        az = FakeAzure(cloud_routes()).on(r"^network private-endpoint list", [{"name": "pe", "states": ["Pending"]}])
        with self.assertRaisesRegex(MvpError, "pe is not approved"):
            self.deploy(az)
        self.assertFalse(self.state.done("B7"))
        self.assertEqual(re.findall(r"/(resume|suspend)\?", "\n".join(az.calls)),
                         ["resume", "suspend", "resume", "suspend"],
                         "B5 supply and B6 aftersales pause Fabric before B7 fails.")
        self.assertFalse(self.state.done("B8"))
        self.assertNotIn("Stage B complete", self.printed)

    def test_import_and_supplier_wait_for_the_expected_revision_to_receive_all_traffic(self):
        old_ready = {**APP_STATUS, "readyRevision": "mvp01-passport--old"}
        az = FakeAzure(cloud_routes()).on(r"^containerapp show .*--query " + re.escape(deploy.APP_STATUS_QUERY),
                                          Seq(old_ready, APP_STATUS))
        traffic = r"^containerapp revision show .*--revision mvp01-passport--new .*properties\.trafficWeight"
        az.on(traffic, Seq(*({**APP_SERVING, "weight": weight} for weight in (0, 50, 100))))

        def post(url, body, token):
            self.assertEqual(len(az.called(traffic)), 3, "a healthy old revision must not receive Stage B requests")
            return self.post(url, body, token)

        self.deploy(az, runtime=self.runtime(az, http_post=post))
        self.assertTrue(self.state.done("B6"))
        self.assertEqual(self.state.outputs("B7")["supplier"], "deferred")

    def test_a_resumed_check_times_out_without_posting_a_task_while_traffic_is_still_switching(self):
        self.deploy(FakeAzure(cloud_routes()))
        self.supplier.calls.clear()
        az = FakeAzure(cloud_routes(app_present=True)).on(r"^containerapp revision show", {**APP_SERVING, "weight": 0})
        clock = itertools.count(0, smoke.READY_TIMEOUT + 1)
        with self.assertRaisesRegex(MvpError, "mvp01-passport--new.*traffic"):
            self.deploy(az, start="B7", runtime=self.runtime(az, clock=lambda: next(clock)))
        self.assertFalse(self.state.done("B7"))
        self.assertEqual(self.supplier.calls, [])
        self.assertEqual(self.state.counter(deploy.SMOKE_COUNTER), 0)

    def test_ready_revision_with_an_older_image_cannot_receive_the_smoke_call(self):
        az = FakeAzure(cloud_routes()).on(r"^containerapp revision show", {**APP_SERVING, "images": ["old-image"]})
        with self.assertRaisesRegex(MvpError, "does not report the expected passport image"):
            self.deploy(az)
        self.assertFalse(self.state.done("B6"))
        self.assertEqual(self.supplier.calls, [])


class AppRedeployTests(Stage):
    def setUp(self):
        super().setUp()
        self.state.bind(self.config.identity)
        for phase in deploy.ORDER:
            self.state.record(phase, f"original-{phase}", {"retained": phase})
        self.state.amend("B3", {"image": "mvp01acr.azurecr.io/passport-app:previous", "appUrl": APP_URL,
                                "agent_extra": "keep-agent-settings", "sign_in_expires": "2026-12-12"})
        self.before = self.state.path.read_text(encoding="utf-8")
        self.image = "mvp01acr.azurecr.io/passport-app:20261112-080000"
        self.current = {"mode": "Single", "fqdn": APP_URL[8:], "state": "Succeeded",
                        "revision": "mvp01-passport--old", "readyRevision": "mvp01-passport--old",
                        "containers": [{"name": "passport", "image": self.state.outputs("B3")["image"]}]}
        self.updated = {**self.current, "revision": "mvp01-passport--new", "readyRevision": "mvp01-passport--new",
                        "containers": [{"name": "passport", "image": self.image}]}

    def azure(self):
        return FakeAzure(cloud_routes()).on(r"^containerapp show", Seq(self.current, self.updated)).on(
            r"^containerapp update", self.updated["revision"])

    def redeploy(self, az, **overrides):
        return deploy.app_redeploy(self.config, az, self.state, runtime=self.runtime(az, **overrides), out=self.out)

    def test_builds_and_updates_only_the_image_then_waits_for_the_new_revision(self):
        waiting = {**self.updated, "readyRevision": self.current["revision"]}
        az = self.azure().on(r"^containerapp show", Seq(self.current, waiting, self.updated))

        def healthy(url):
            self.assertEqual(len(az.called(r"^containerapp show")), 3)
            self.assertEqual(url, f"{APP_URL}/health/ready")
            return ready(url)

        result = self.redeploy(az, http_get=healthy)
        self.assertEqual(result["image"], self.image)
        self.assertEqual(az.called(r"^acr build"),
                         [f"acr build --registry mvp01acr --image passport-app:20261112-080000 {image.APP}"])
        self.assertEqual(az.called(r"^containerapp update"), [
            "containerapp update --resource-group mvp01-rg --name mvp01-passport --container-name passport "
            f"--image {self.image} --query properties.latestRevisionName"])
        self.assertLess(az.index(r"^acr repository delete"), az.index(r"^acr build"))
        allowed = (r"^(account show|ad signed-in-user show|deployment sub show|"
                   r"acr (show|build|repository (list|delete))|"
                   r"containerapp (show|update|revision show)) ")
        self.assertTrue(all(re.match(allowed, call) for call in az.calls), az.calls)
        self.assertEqual(self.sleeps, [smoke.READY_INTERVAL])
        expected = json.loads(self.before)
        expected["phases"]["B3"]["outputs"]["image"] = self.image
        self.assertEqual(self.state.data, expected)
        self.assertEqual((self.database.opened, self.blobs.uploads, self.fabric.runs, self.foundry.opened,
                          self.supplier.calls), ([], [], [], [], []))
        self.assertFalse(self.runtime(az).output.exists())

    def test_missing_state_or_app_and_multiple_revision_mode_are_refused_before_build(self):
        for response in (NOT_FOUND, {**self.current, "mode": "Multiple"}, {**self.current, "containers": []}):
            with self.subTest(response=response):
                az = self.azure().on(r"^containerapp show", response)
                with self.assertRaises(MvpError):
                    self.redeploy(az)
                self.assertEqual(az.called(r"^acr|^containerapp update"), [])
                self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)
        self.state.forget_from("B3", deploy.ORDER)
        az = self.azure()
        with self.assertRaisesRegex(MvpError, "no deployed app image"):
            self.redeploy(az)
        self.assertEqual(az.called(r"^acr|^containerapp"), [])

    def test_wrong_subscription_is_refused_before_build_or_update(self):
        az = self.azure()
        for pattern, response in session_routes(subscription="11111111-1111-1111-1111-111111111111"):
            az.on(pattern, response)
        with self.assertRaisesRegex(MvpError, "az account set"):
            self.redeploy(az)
        self.assertEqual(az.called(r"^deployment|^acr|^containerapp"), [])
        self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)

    def test_cleanup_build_or_update_failure_keeps_the_previous_recorded_image(self):
        for command in (r"^acr repository list", r"^acr repository delete", r"^acr build", r"^containerapp update"):
            with self.subTest(command=command):
                az = self.azure().on(command, Fail("ERROR: Operation failed"))
                with self.assertRaisesRegex(MvpError, "Operation failed"):
                    self.redeploy(az)
                self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)
                if command != r"^containerapp update":
                    self.assertEqual(az.called(r"^containerapp update"), [])
                if command.startswith(r"^acr repository"):
                    self.assertEqual(az.called(r"^acr build"), [])
                self.assertNotIn("App-only redeploy complete", self.printed)

    def test_missing_or_unchanged_revision_is_not_reported_as_success(self):
        for revision in (None, self.current["revision"]):
            with self.subTest(revision=revision):
                az = self.azure().on(r"^containerapp update", revision)
                with self.assertRaisesRegex(MvpError, "did not report a new revision"):
                    self.redeploy(az)
                self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)

    def test_old_healthy_revision_cannot_hide_a_failed_or_superseded_rollout(self):
        cases = [
            ({**self.updated, "readyRevision": self.current["revision"]}, "did not become ready"),
            ({**self.updated, "state": "Failed"}, "failed"),
            ({**self.updated, "revision": "mvp01-passport--other"}, "changed during redeployment"),
            ({**self.updated, "containers": self.current["containers"]}, "changed during redeployment"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                az = self.azure().on(r"^containerapp show", Seq(self.current, response))
                clock = iter(range(0, 2000, smoke.READY_TIMEOUT + 1))
                get = mock.Mock(side_effect=AssertionError("Must not check the old revision's health"))
                with self.assertRaisesRegex(MvpError, message):
                    self.redeploy(az, clock=lambda clock=clock: next(clock), http_get=get)
                get.assert_not_called()
                self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)

    def test_readiness_failure_does_not_suggest_the_data_writing_b7_check(self):
        clock = iter(range(0, 2000, smoke.READY_TIMEOUT + 1))
        with self.assertRaisesRegex(MvpError, "schema_mismatch.*python -m dbp_mvp app redeploy") as failure:
            self.redeploy(self.azure(), clock=lambda: next(clock),
                          http_get=lambda _: (503, '{"error":"schema_mismatch"}'))
        self.assertNotIn("--from B7", str(failure.exception))
        self.assertEqual(self.state.path.read_text(encoding="utf-8"), self.before)


class AppPricesTests(Stage):
    def setUp(self):
        super().setUp()
        self.config = make_config(prices={"currency": "USD", "models": {
            "gpt-6.1-sol": {"input_per_million": 2, "output_per_million": 3}}})
        self.encoded = deploy.prices_parameter(self.config)["pricesB64"]
        self.current = {"mode": "Single", "fqdn": APP_URL[8:], "state": "Succeeded",
                        "revision": "mvp01-passport--old", "readyRevision": "mvp01-passport--old",
                        "containers": [{"name": "passport", "image": "mvp01acr.azurecr.io/passport-app:existing",
                                        "prices": []}]}
        self.updated = {**self.current, "revision": "mvp01-passport--prices", "readyRevision": "mvp01-passport--prices",
                        "containers": [{**self.current["containers"][0],
                                        "prices": [{"name": "DBP_PRICES_B64", "value": self.encoded}]}]}

    def azure(self):
        serving = {"weight": 100, "images": [self.current["containers"][0]["image"]]}
        return FakeAzure(session_routes()).on(r"^group show", {
            "location": self.config.location, "tags": {"project": "battery-passport"}
        }).on(r"^containerapp revision show", serving).on(r"^containerapp show", Seq(self.current, self.updated)).on(
            r"^containerapp update", self.updated["revision"])

    def apply_prices(self, az, **overrides):
        return deploy.app_prices(self.config, az, runtime=self.runtime(az, **overrides), out=self.out)

    def test_price_update_needs_no_vm_state_build_or_other_deployment_and_checks_the_new_revision(self):
        waiting = {**self.updated, "readyRevision": self.current["revision"]}
        az = self.azure().on(r"^containerapp show", Seq(self.current, waiting, self.updated))

        def healthy(url):
            self.assertEqual(len(az.called(r"^containerapp show")), 3)
            self.assertEqual(url, f"{APP_URL}/health/ready")
            return ready(url)

        result = self.apply_prices(az, http_get=healthy)
        self.assertEqual(result["revision"], self.updated["revision"])
        self.assertEqual(az.called(r"^containerapp update"), [
            "containerapp update --resource-group mvp01-rg --name mvp01-passport --container-name passport "
            f"--set-env-vars DBP_PRICES_B64={self.encoded} --query properties.latestRevisionName"])
        allowed = r"^(account show|ad signed-in-user show|group show|containerapp (show|update|revision show)) "
        self.assertTrue(all(re.match(allowed, call) for call in az.calls), az.calls)
        self.assertEqual(self.sleeps, [smoke.READY_INTERVAL])
        self.assertFalse(self.state.path.exists())
        self.assertEqual((self.database.opened, self.blobs.uploads, self.fabric.runs, self.foundry.opened,
                          self.supplier.calls), ([], [], [], [], []))
        self.assertIn("Model prices configured (USD)", self.printed)

    def test_reapplying_the_same_file_checks_readiness_without_creating_another_revision(self):
        az = self.azure().on(r"^containerapp show", self.updated)
        self.apply_prices(az)
        self.assertEqual(az.called(r"^containerapp update"), [])
        self.assertIn("prices are unchanged", self.printed)

    def test_missing_prices_wrong_environment_or_missing_app_are_refused_before_any_update(self):
        for command, response in (
                (r"^account show", {"id": "11111111-1111-1111-1111-111111111111"}),
                (r"^group show", NOT_FOUND),
                (r"^group show", {"location": self.config.location, "tags": {}}),
                (r"^group show", {"location": "westeurope", "tags": {"project": "battery-passport"}}),
                (r"^containerapp show", NOT_FOUND),
                (r"^containerapp show", {**self.current, "mode": "Multiple"}),
                (r"^containerapp show", {**self.current, "containers": []})):
            with self.subTest(command=command, response=response):
                az = self.azure().on(command, response)
                with self.assertRaises(MvpError):
                    self.apply_prices(az)
                self.assertEqual(az.called(r"^containerapp update"), [])
        self.config = make_config()
        az = self.azure()
        with self.assertRaisesRegex(MvpError, "No model prices configured"):
            self.apply_prices(az)
        self.assertEqual(az.calls, [])

    def test_update_failure_or_missing_new_revision_is_not_reported_as_success(self):
        for response in (Fail("ERROR: Operation failed"), None, self.current["revision"]):
            with self.subTest(response=response):
                az = self.azure().on(r"^containerapp update", response)
                with self.assertRaises(MvpError):
                    self.apply_prices(az)
                self.assertNotIn("Model prices configured", self.printed)

    def test_rollout_must_serve_the_requested_prices_on_the_same_image(self):
        wrong_prices = [{**self.updated["containers"][0], "prices": []}]
        wrong_image = [{**self.updated["containers"][0], "image": "mvp01acr.azurecr.io/passport-app:other"}]
        cases = [
            ({**self.updated, "readyRevision": self.current["revision"]}, "did not become ready"),
            ({**self.updated, "state": "Failed"}, "failed.*app prices"),
            ({**self.updated, "revision": "other"}, "changed during redeployment"),
            ({**self.updated, "containers": wrong_prices}, "changed during redeployment"),
            ({**self.updated, "containers": wrong_image}, "changed during redeployment"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                az = self.azure().on(r"^containerapp show", Seq(self.current, response))
                clock = iter(range(0, 2000, smoke.READY_TIMEOUT + 1))
                get = mock.Mock(side_effect=AssertionError("Must not accept the previous revision's health"))
                with self.assertRaisesRegex(MvpError, message):
                    self.apply_prices(az, clock=lambda clock=clock: next(clock), http_get=get)
                get.assert_not_called()
                self.assertNotIn("Model prices configured", self.printed)

    def test_failed_health_check_points_to_the_same_price_command_not_a_build_or_stage_b(self):
        clock = iter(range(0, 2000, smoke.READY_TIMEOUT + 1))
        with self.assertRaisesRegex(MvpError, "schema_mismatch.*app prices.*--file") as failure:
            self.apply_prices(self.azure(), clock=lambda: next(clock),
                              http_get=lambda _: (503, '{"error":"schema_mismatch"}'))
        self.assertNotIn("app redeploy", str(failure.exception))
        self.assertNotIn("--from B7", str(failure.exception))
        self.assertNotIn("Model prices configured", self.printed)


class AgentTests(Stage):
    def test_clean_redeploy_skips_supplier_smoke_to_leave_the_fresh_database_clean(self):
        self.deploy(FakeAzure(cloud_routes()), skip_supplier_check=True)

        self.assertEqual(self.supplier.calls, [])
        self.assertEqual((self.state.outputs("B7")["supplier"], self.state.outputs("B7")["telemetry"]),
                         ("not run (clean reset)", "not checked"))
        self.assertIn("The clean reset skipped the Supplier smoke call", self.printed)

    def test_b4_creates_the_agents_then_switches_the_app_to_agent_mode(self):
        az = FakeAzure(cloud_routes())
        self.deploy(az)

        first, second = self.app_deployments(az)
        self.assertFalse(first["agentMode"]["value"])
        self.assertTrue(second["agentMode"]["value"])
        self.assertEqual(second["image"], first["image"])
        self.assertEqual(second["extraAllowedClientIds"]["value"], sorted(AGENT_CLIENTS.values()))
        settings = json.loads(base64.b64decode(second["foundryConfigB64"]["value"]))
        self.assertEqual(settings["project_endpoint"], INFRA["foundryProjectEndpoint"])
        self.assertEqual({role: entry["kind"] for role, entry in settings["roles"].items()},
                         {role: spec["kind"] for role, spec in ROLES.items()})
        runtime = data_plane.app_module("foundry_runtime")
        for role, protocol in (("supplier", "responses"), ("lifecycle", "invocations")):
            entry = settings["roles"][role]
            self.assertEqual(entry["principal_id"], AGENT_PRINCIPALS[role])
            self.assertEqual(runtime.hosted_endpoint(settings["project_endpoint"], entry["name"],
                                                     entry["endpoint"])[0], protocol)

        self.assertEqual(self.foundry.uploads, ["dbp-supplier", "dbp-lifecycle"])
        self.assertEqual(self.foundry.created, ["dbp-carbon", "dbp-issuance", "dbp-watch"])
        self.assertEqual(self.foundry.polled, ["dbp-supplier", "dbp-lifecycle"])  # each hosted build polled once
        app_roles = json.loads([content for call, _, content in az.files if "--method patch" in call][-1])["appRoles"]
        self.assertEqual(sorted(role["value"] for role in app_roles if role["allowedMemberTypes"] == ["Application"]),
                         ["Passport.Agent.Lifecycle", "Passport.Agent.Supplier"])
        granted = [json.loads(content) for call, _, content in az.files if "/appRoleAssignments --headers" in call]
        self.assertEqual([(item["principalId"], item["resourceId"]) for item in granted],
                         [(AGENT_PRINCIPALS["supplier"], SP), (AGENT_PRINCIPALS["lifecycle"], SP)])
        self.assertIn(f"--assignee-object-id {PROJECT_PRINCIPAL} --assignee-principal-type ServicePrincipal",
                      az.called(r"^role assignment create")[0])
        deployments = [i for i, call in enumerate(az.calls) if call.startswith("deployment group create")]
        self.assertLess(az.index(r"^cognitiveservices account show"), az.index(r"^role assignment create"))
        self.assertLess(az.index(r"/appRoleAssignments --headers"), deployments[1])

        key = self.state.outputs("B4")["smoke_key"]
        self.assertEqual(self.supplier.calls, [(f"{APP_URL}/agents/supplier/run",
                                                {"task_id": f"smoke-{key}-0", "subject": smoke.SMOKE_LOT}, TOKEN)])
        self.assertTrue(az.called(rf"get-access-token --scope api://{APP_ID}/user_impersonation"))
        self.assertEqual((self.state.outputs("B7")["supplier"], self.state.outputs("B7")["telemetry"]),
                         ("deferred", "found"))
        self.assertEqual(self.state.outputs("B3")["agent_extra"], self.state.outputs("B4")["extra_fingerprint"])
        self.assertNotIn(TOKEN, self.state.path.read_text(encoding="utf-8") + self.printed)
        self.assertIn("Agents: Foundry agents supplier v1, carbon v1, issuance v1, lifecycle v1, watch v1; the "
                      "Supplier agent check deferred.", self.printed)
        self.assertNotIn("Warning", self.printed)

    def test_a_new_app_image_keeps_agent_mode_and_the_agent_versions(self):
        self.deploy(FakeAzure(cloud_routes()))
        settings = self.state.outputs("B4")["app_extra"]
        az = FakeAzure(cloud_routes(app_present=True))
        self.deploy(az, start="B3")

        (deployed,) = self.app_deployments(az)
        self.assertTrue(deployed["agentMode"]["value"])
        self.assertEqual(deployed["foundryConfigB64"]["value"], settings["foundryConfigB64"])
        self.assertEqual(self.foundry.uploads, ["dbp-supplier", "dbp-lifecycle"])
        self.assertEqual(self.state.outputs("B3")["agent_extra"], self.state.outputs("B4")["extra_fingerprint"])

    def test_changed_agent_source_builds_new_hosted_versions_only(self):
        self.deploy(FakeAzure(cloud_routes()))
        code, _ = foundry.package()
        az = FakeAzure(cloud_routes(app_present=True))
        with mock.patch.object(foundry, "package", return_value=(code, "0" * 64)):
            self.deploy(az)

        self.assertEqual(self.foundry.uploads, ["dbp-supplier", "dbp-lifecycle"] * 2)
        self.assertEqual(self.foundry.created, ["dbp-carbon", "dbp-issuance", "dbp-watch"])
        self.assertEqual(self.state.outputs("B4")["versions"],
                         {"supplier": "2", "carbon": "1", "issuance": "1", "lifecycle": "2", "watch": "1"})
        (deployed,) = self.app_deployments(az)
        settings = json.loads(base64.b64decode(deployed["foundryConfigB64"]["value"]))
        self.assertEqual(settings["roles"]["supplier"]["version"], "2")
        first, second = (body["task_id"] for _, body, _ in self.supplier.calls)
        self.assertNotEqual(first, second)

    def test_a_failed_supplier_build_reports_its_cause_and_stops_before_later_agents(self):
        self.foundry.after_build = "failed"
        self.foundry.build_error = {"code": "CodeError", "message": "No Python dependency manifest found."}
        az = FakeAzure(cloud_routes())
        with self.assertRaisesRegex(MvpError, "CodeError: No Python dependency manifest found"):
            self.deploy(az)

        self.assertEqual(self.foundry.uploads, ["dbp-supplier"])
        self.assertEqual(self.foundry.created, [])
        self.assertEqual(az.called(r"/appRoleAssignments --headers"), [])
        self.assertFalse(self.state.done("B4"))
        (deployed,) = self.app_deployments(az)
        self.assertFalse(deployed["agentMode"]["value"])

    def test_a_failed_supplier_check_moves_to_a_new_task(self):
        self.supplier.answers = [(502, {"error": "agent_execution_failed", "message": "The agent run failed."})]
        with self.assertRaisesRegex(MvpError, "(?s)agent_execution_failed.*hosted-agent-runtime-failures") as raised:
            self.deploy(FakeAzure(cloud_routes()))
        self.assertNotIn("enable_nat_gateway", str(raised.exception))
        self.assertFalse(self.state.done("B7"))

        self.supplier.answers = [(422, {"error": "citation_required", "message": "Cite the evidence."})]
        self.deploy(FakeAzure(cloud_routes(app_present=True)))
        first, second = (body["task_id"] for _, body, _ in self.supplier.calls)
        self.assertEqual((first[-2:], second[-2:]), ("-0", "-1"))
        self.assertEqual(self.state.outputs("B7")["supplier"], "refused an uncited decision")

    def test_supplier_check_accepts_a_cited_no_issue_and_fails_a_review_on_the_claim(self):
        cited = {"action": "no_issue", "evidence_ids": ["EV-1"]}
        self.supplier.answers = [(200, {"decision": {**cited, "action": "request_review"},
                                        "outcome": {**cited, "action": "request_review"}}),
                                 (200, {"decision": cited, "outcome": cited})]
        with self.assertRaisesRegex(MvpError, f"request_review for {smoke.SMOKE_LOT}"):
            self.deploy(FakeAzure(cloud_routes()))
        self.assertFalse(self.state.done("B7"))

        self.deploy(FakeAzure(cloud_routes(app_present=True)))
        self.assertEqual(self.state.outputs("B7")["supplier"], "found no issue")
        self.assertIn("the Supplier agent check found no issue.", self.printed)

    def test_missing_telemetry_warns_and_is_queried_once_on_a_rerun(self):
        clock = itertools.count(0, 60)
        az = FakeAzure(cloud_routes()).on(r"applicationinsights", TELEMETRY_EMPTY)
        self.deploy(az, runtime=self.runtime(az, clock=lambda: next(clock)))

        self.assertEqual(self.state.outputs("B7")["telemetry"], "missing")
        self.assertGreater(len(az.called(r"applicationinsights")), 1)
        self.assertIn("no agent.run event of the Supplier agent reached Application Insights", self.printed)
        self.assertIn("Rerun 'python -m dbp_mvp deploy --from B7' in a few minutes", self.printed)

        az = FakeAzure(cloud_routes(app_present=True))
        self.lines.clear()
        self.deploy(az)
        self.assertEqual(len(az.called(r"applicationinsights")), 1)
        self.assertEqual(self.state.outputs("B7")["telemetry"], "found")
        self.assertNotIn("Warning", self.printed)


class EvaluationTests(Stage):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(deploy, "OUTPUT", self.state.path.parent))

    def test_without_apply_only_the_scoring_code_is_checked(self):
        az = FakeAzure()
        report = deploy.evaluation(self.config, az, runtime=self.runtime(az), out=self.out)
        self.assertEqual(az.calls, [])
        self.assertTrue(all(row["score"]["pass"] for row in report["rows"]))
        self.assertTrue((self.state.path.parent / "evaluation" / "local" / "report.json").is_file())
        self.assertIn("not of a model", self.printed)

    def test_apply_needs_the_scenario_switch(self):
        az = FakeAzure(cloud_routes())
        with self.assertRaisesRegex(MvpError, "evaluation = true"):
            deploy.evaluation(self.config, az, apply=True, runtime=self.runtime(az), out=self.out)
        self.assertEqual(az.calls, [])

    def test_apply_compares_the_deployed_models(self):
        calls, report = [], {"summary": {"baseline": {"passed": 9, "cases": 9, "critical_failures": 0,
                                                      "decision": "keep"}}, "fatal_error": None}

        def run_models(endpoint, credential, output, repeats, *, project_factory, prices=None, assignments=None):
            calls.append((endpoint, credential, output, repeats, project_factory(endpoint, credential)))
            return report

        module = SimpleNamespace(run_models=run_models, failed=lambda result: bool(result["fatal_error"]))
        az = FakeAzure(cloud_routes())
        runtime = self.runtime(az, evaluation=lambda: module, credential=lambda: "cli-credential")
        config = make_config(scenarios={"evaluation": True})
        deploy.evaluation(config, az, apply=True, repeats=2, runtime=runtime, out=self.out)

        self.assertEqual(calls, [(INFRA["foundryProjectEndpoint"], "cli-credential",
                                  self.state.path.parent / "evaluation" / "models", 2, self.foundry)])
        self.assertTrue(az.called(r"^cognitiveservices account deployment list"))
        self.assertIn("baseline      9/9 passed, 0 critical failures, decision keep", self.printed)
        report["fatal_error"] = "Model deployment gpt-5.4 was not found."
        with self.assertRaisesRegex(MvpError, "evaluation found failures"):
            deploy.evaluation(config, az, apply=True, runtime=runtime, out=self.out)

    def test_shield_screens_the_probes_without_the_models(self):
        summary = {"attacks_detected": 9, "attacks": 10, "false_positives": 1, "clean": 9, "errors": 0,
                   "decision": "review_misses"}
        calls = []

        def run_shield(endpoint, credential, output):
            calls.append((endpoint, credential, output))
            return {"summary": {"prompt_shields": summary}}

        module = SimpleNamespace(run_shield=run_shield)
        az = FakeAzure(cloud_routes())
        runtime = self.runtime(az, evaluation=lambda: module, credential=lambda: "cli-credential")
        config = make_config(scenarios={"evaluation": True})
        with self.assertRaisesRegex(MvpError, "--shield belongs"):
            deploy.evaluation(config, az, shield=True, runtime=runtime, out=self.out)
        deploy.evaluation(config, az, apply=True, shield=True, runtime=runtime, out=self.out)

        self.assertEqual(calls, [(INFRA["foundryProjectEndpoint"], "cli-credential",
                                  self.state.path.parent / "evaluation" / "shield")])
        self.assertFalse(az.called(r"^cognitiveservices account deployment list"))
        self.assertIn("9/10 attacks detected, 1/9 benign notes flagged, 0 errors", self.printed)
        summary["errors"] = 2
        with self.assertRaisesRegex(MvpError, "Cognitive Services User"):
            deploy.evaluation(config, az, apply=True, shield=True, runtime=runtime, out=self.out)


class CredentialNegativesTests(Stage):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(deploy, "OUTPUT", self.state.path.parent))

    def test_the_local_run_gets_every_expected_refusal(self):
        report = deploy.credential_negatives(self.config, None, self.state, out=self.out)
        self.assertEqual({row["result"] for row in report["checks"]}, {"PASS"})
        self.assertEqual({row["expected"].split()[-1] for row in report["checks"]} - {"(replayed)"},
                         {"bad_signature", "issuer_mismatch", "wrong_audience", "expired", "revoked",
                          "message_conflict", "lot_not_allowed", "event_type_not_allowed", "unknown_key"})
        self.assertIn("10 of 10 credential checks passed (mock connector", self.printed)
        self.assertTrue((self.state.path.parent / "credential_negatives" / "local" / "report.json").is_file())

    def test_apply_needs_the_scenario_switch(self):
        az = FakeAzure(cloud_routes())
        with self.assertRaisesRegex(MvpError, "append-only connector log.*credential_negatives = true"):
            deploy.credential_negatives(self.config, az, self.state, apply=True, runtime=self.runtime(az),
                                        out=self.out)
        self.assertEqual(az.calls, [])


# ---- Microsoft Entra ID ------------------------------------------------------------------------------------------

class IdentityTests(unittest.TestCase):
    def test_a_registration_without_the_marker_is_refused(self):
        az = FakeAzure(graph_routes(apps=[{"id": APP_OBJECT, "appId": APP_ID, "tags": []}]))
        with self.assertRaisesRegex(MvpError, "not created by this MVP"):
            identity.ensure_registration(az, "mvp01-passport", "app.example", sleep=lambda _: None)
        self.assertEqual(az.called(r"--method (post|patch)"), [])

    def test_duplicate_registrations_are_refused(self):
        az = FakeAzure(graph_routes(apps=[{"id": "1", "tags": [MARKER]}, {"id": "2", "tags": [MARKER]}]))
        with self.assertRaisesRegex(MvpError, "More than one"):
            identity.ensure_registration(az, "mvp01-passport", "app.example", sleep=lambda _: None)

    def test_the_operator_becomes_owner_of_the_app_and_its_principal_once(self):
        owners = rf"--method post --url {GRAPH}/(applications/{APP_OBJECT}|servicePrincipals/{SP})/owners/\$ref"
        az = FakeAzure(graph_routes(owners=["someone-else"]))
        identity.ensure_registration(az, "mvp01-passport", "app.example", sleep=lambda _: None, owner_id=OPERATOR)
        self.assertEqual(len(az.called(owners)), 2)
        self.assertIn(f"{identity.GRAPH}/directoryObjects/{OPERATOR}", az.file(owners))
        az = FakeAzure(graph_routes(owners=[OPERATOR.upper()]))
        identity.ensure_registration(az, "mvp01-passport", "app.example", sleep=lambda _: None, owner_id=OPERATOR)
        self.assertEqual(az.called(r"/owners/\$ref"), [])

    def test_manifest_ids_are_stable_and_foreign_entries_are_kept(self):
        first, _ = identity.manifest(APP_ID, "app.example")
        again, _ = identity.manifest(APP_ID, "app.example")
        self.assertEqual(first["appRoles"], again["appRoles"])
        existing = {"appRoles": [{"id": "kept-id", "value": "Passport.Operator"}, {"id": "x", "value": "Other"}]}
        updated, second = identity.manifest(APP_ID, "app.example", existing)
        roles = {role["value"]: role["id"] for role in updated["appRoles"]}
        self.assertEqual(roles["Passport.Operator"], "kept-id")
        self.assertIn("Other", roles)
        scope_id = updated["api"]["oauth2PermissionScopes"][0]["id"]
        self.assertEqual(second["api"]["preAuthorizedApplications"],
                         [{"appId": identity.AZURE_CLI_APP_ID, "delegatedPermissionIds": [scope_id]}])
        self.assertEqual(updated["web"]["redirectUris"], ["https://app.example/.auth/login/aad/callback"])

    def test_only_missing_roles_are_assigned(self):
        az = FakeAzure(graph_routes(assigned=["Passport.Operator", "Passport.Admin"]))
        added, held = identity.assign_roles(az, SP, OPERATOR, sleep=lambda _: None)
        self.assertEqual(added, ["Passport.Reviewer", "Passport.Connector", "Passport.Compliance"])
        self.assertEqual(held, ["Passport.Admin", "Passport.Compliance", "Passport.Connector", "Passport.Operator",
                                "Passport.Reviewer"])
        self.assertIn('"r-reviewer"', az.file(r"appRoleAssignedTo --headers"))

    def test_viewer_roles_are_declared_but_not_given_to_the_operator(self):
        first, _ = identity.manifest(APP_ID, "app.example")
        self.assertEqual({role["value"] for role in first["appRoles"]},
                         {"Passport.Operator", "Passport.Reviewer", "Passport.Connector", "Passport.Compliance",
                          "Passport.Admin", "Passport.OEM", "Passport.Recycler", "Passport.Authority",
                          "Passport.PolicyApprover", "Passport.Fleet.AdventureWorks", "Passport.Fleet.Fabrikam"})
        az = FakeAzure(graph_routes())
        added, _ = identity.assign_roles(az, SP, OPERATOR, sleep=lambda _: None)
        self.assertFalse(set(added) & set(identity.VIEWER_ROLES))

    def test_assigning_and_removing_roles_is_idempotent(self):
        az = FakeAzure(graph_routes(assigned=["Passport.OEM"]))
        changed, held = identity.change_roles(az, SP, OPERATOR, ["Passport.OEM", "Passport.Recycler"],
                                              sleep=lambda _: None)
        self.assertEqual((changed, held), (["Passport.Recycler"], ["Passport.OEM", "Passport.Recycler"]))
        self.assertIn('"r-recycler"', az.file(r"appRoleAssignedTo --headers"))
        self.assertEqual(len(az.called(r"--method post")), 1)
        changed, held = identity.change_roles(az, SP, OPERATOR, ["Passport.OEM", "Passport.Authority"], remove=True,
                                              sleep=lambda _: None)
        self.assertEqual((changed, held), (["Passport.OEM"], []))
        self.assertEqual(len(az.called(r"--method delete .*/appRoleAssignedTo/a-r-oem")), 1)

    def test_unknown_roles_and_users_are_refused(self):
        az = FakeAzure(graph_routes())
        with self.assertRaisesRegex(MvpError, r"Passport\.Owner is not a role of this MVP\. Choose from "
                                              r"Passport\.Operator, .*Passport\.OEM"):
            identity.change_roles(az, SP, OPERATOR, ["Passport.Owner"], sleep=lambda _: None)
        self.assertEqual(az.calls, [])
        az = FakeAzure([(rf"--method get --url {GRAPH}/users/nobody ", NOT_FOUND)])
        with self.assertRaisesRegex(MvpError, "No user nobody in this Microsoft Entra tenant"):
            identity.find_user(az, "nobody", sleep=lambda _: None)
        az = FakeAzure(graph_routes())
        self.assertEqual(identity.find_user(az, sleep=lambda _: None), (OPERATOR, "operator"))

    def test_graph_retries_while_a_new_object_replicates(self):
        sleeps = []
        az = FakeAzure([(r"servicePrincipals", Seq(Fail("ERROR: Not Found({...})"), {"value": []}))])
        self.assertEqual(identity.graph(az, "get", "/servicePrincipals", sleep=sleeps.append), {"value": []})
        self.assertEqual(sleeps, [5])
        az = FakeAzure([(r"applications", Fail("ERROR: Forbidden: Insufficient privileges"))])
        with self.assertRaisesRegex(MvpError, "Insufficient privileges"):
            identity.graph(az, "get", "/applications", sleep=sleeps.append)


# ---- image, database, checks -------------------------------------------------------------------------------------

class ImageTests(unittest.TestCase):
    def test_removes_all_previous_app_images_before_building_without_touching_other_repositories(self):
        az = FakeAzure(cloud_routes()).on(r"^acr repository list", ["passport-app", "other-app"])
        chosen = image.build(az, "mvp01-rg", "mvp01acr", "mvp01acr.azurecr.io", NOW)
        self.assertEqual(chosen, "mvp01acr.azurecr.io/passport-app:20261112-080000")
        self.assertEqual(az.called(r"^acr repository delete"), [
            "acr repository delete --name mvp01acr --repository passport-app --yes"])
        self.assertLess(az.index(r"^acr show"), az.index(r"^acr repository list"))
        self.assertLess(az.index(r"^acr repository list"), az.index(r"^acr repository delete"))
        self.assertLess(az.index(r"^acr repository delete"), az.index(r"^acr build"))

    def test_first_build_or_retry_after_deleted_repository_still_builds(self):
        for repositories in ([], ["other-app"]):
            with self.subTest(repositories=repositories):
                az = FakeAzure(cloud_routes()).on(r"^acr repository list", repositories)
                image.build(az, "mvp01-rg", "mvp01acr", "mvp01acr.azurecr.io", NOW)
                self.assertEqual(az.called(r"^acr repository delete"), [])
                self.assertEqual(len(az.called(r"^acr build")), 1)

    def test_an_unexpected_registry_is_refused_before_building(self):
        az = FakeAzure([(r"^acr show", {"access": "Enabled", "sku": "Standard", "admin": True})])
        with self.assertRaisesRegex(MvpError, "admin user"):
            image.build(az, "mvp01-rg", "mvp01acr", "mvp01acr.azurecr.io", NOW)
        self.assertEqual(az.called(r"^acr build"), [])
        self.assertEqual(az.called(r"^acr repository"), [])

    def test_images_must_come_from_the_mvp_registry_with_a_tag(self):
        self.assertEqual(image.check_image("mvp01acr.azurecr.io/passport-app:v1", "mvp01acr.azurecr.io"),
                         "mvp01acr.azurecr.io/passport-app:v1")
        for wrong in ("docker.io/passport-app:v1", "mvp01acr.azurecr.io/passport-app",
                      "mvp01acr.azurecr.io/Passport:v1", "mvp01acr.azurecr.io.evil.example/passport-app:v1"):
            with self.subTest(wrong), self.assertRaises(MvpError):
                image.check_image(wrong, "mvp01acr.azurecr.io")


class DataPlaneTests(unittest.TestCase):
    def setUp(self):
        self.database, self.blobs, self.sleeps = Database(), Blobs(), []
        self.connection = self.database.connect(INFRA, None, None)

    def test_rows_rejects_a_column_count_mismatch_and_closes_the_cursor(self):
        cursor = mock.Mock(description=[("first",), ("second",)])
        cursor.fetchall.return_value = [(1,)]
        connection = mock.Mock()
        connection.cursor.return_value = cursor
        with self.assertRaises(ValueError):
            data_plane.rows(connection, "SELECT values")
        cursor.close.assert_called_once()

    def test_scripts_split_on_go_and_grants_are_validated(self):
        self.assertEqual(data_plane.sql_batches("-- note\nGO\nSELECT 1\n go \n-- only\nGO\nSELECT 2"),
                         ["SELECT 1", "SELECT 2"])
        script = data_plane.grant_script("mvp01-app-id", INFRA["appIdentityClientId"])
        self.assertNotIn("__APP_", script)
        self.assertIn("GRANT SELECT, INSERT ON OBJECT::dbp.PassportVersions TO [mvp01-app-id];", script)
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON OBJECT::dbp.PassportState TO [mvp01-app-id];", script)
        grants = [line for line in script.splitlines() if line.startswith("GRANT")]
        self.assertFalse([line for line in grants if re.search(r"DELETE|ON SCHEMA|report\.", line)])
        for name, client in (("bad]name", INFRA["appIdentityClientId"]), ("mvp01-app-id", "not-a-guid")):
            with self.subTest(name=name, client=client), self.assertRaises(MvpError):
                data_plane.grant_script(name, client)

    def test_anchor_stores_the_digest_and_verifies_every_digest(self):
        first = data_plane.anchor(self.connection, INFRA, self.blobs, self.sleeps.append, out=lambda *_: None)
        second = data_plane.anchor(self.connection, INFRA, self.blobs, self.sleeps.append, out=lambda *_: None)
        self.assertEqual((first["digests"], second["digests"]), (1, 2))
        self.assertEqual(second["verified_block"], second["block_id"])
        prefix = "manual/mvp01-sql/passport/20261023T080000Z/"
        self.assertTrue(all(blob.startswith(prefix) for blob in self.blobs.items))

    def test_a_recreated_database_starts_a_new_digest_incarnation(self):
        data_plane.anchor(self.connection, INFRA, self.blobs, out=lambda *_: None)
        self.database.created = datetime(2026, 11, 2, 7, 30, 0)
        with self.assertRaisesRegex(MvpError, "ledger anchor"):
            data_plane.verify_anchored(self.connection, INFRA, self.blobs, out=lambda *_: None)
        self.assertEqual(data_plane.anchor(self.connection, INFRA, self.blobs, out=lambda *_: None)["digests"], 1)
        self.assertEqual(sorted({blob.split("/")[3] for blob in self.blobs.items}),
                         ["20261023T080000Z", "20261102T073000Z"])

    def test_an_uncovered_block_or_a_failed_verification_is_an_error(self):
        data_plane.anchor(self.connection, INFRA, self.blobs, out=lambda *_: None)
        self.database.verified = 1
        with self.assertRaisesRegex(MvpError, "did not cover block"):
            data_plane.verify_anchored(self.connection, INFRA, self.blobs, out=lambda *_: None)
        self.database.verified, self.database.verify_error = None, DriverError("Ledger verification failed")
        with self.assertRaisesRegex(MvpError, "Ledger verification failed in Azure SQL"):
            data_plane.verify_anchored(self.connection, INFRA, self.blobs, out=lambda *_: None)

    def test_verify_needs_an_anchored_digest(self):
        with self.assertRaisesRegex(MvpError, "ledger anchor"):
            data_plane.verify_anchored(self.connection, INFRA, self.blobs, out=lambda *_: None)

    def test_storage_403_is_retried_while_the_role_assignment_applies(self):
        self.blobs.forbidden = 2
        data_plane.anchor(self.connection, INFRA, self.blobs, self.sleeps.append, out=lambda *_: None)
        self.assertEqual(self.sleeps, [30, 30])
        self.blobs.forbidden = 99
        with self.assertRaisesRegex(MvpError, "Storage Blob Data Contributor"):
            data_plane.read_digests(INFRA, data_plane.digest_prefix(self.connection, INFRA), self.blobs,
                                    self.sleeps.append, out=lambda *_: None)

    def test_a_public_connection_attempt_is_refused_with_a_hint(self):
        class Driver:
            Error = DriverError
            attempts = []

            @classmethod
            def connect(cls, target, **options):
                cls.attempts.append(target)
                if len(cls.attempts) == 1:
                    raise DriverError("Database 'passport' is not currently available (40613)")
                raise DriverError("Client with IP address '20.1.2.3' is not allowed to access the server.")

        with self.assertRaisesRegex(MvpError, "private endpoint"):
            data_plane.connect(INFRA, Driver, credential=object(), sleep=self.sleeps.append, out=lambda *_: None)
        self.assertEqual(len(Driver.attempts), 2)
        self.assertIn("Server=tcp:mvp01-sql.database.windows.net,1433", Driver.attempts[0])


class SmokeTests(unittest.TestCase):
    def test_names_must_resolve_to_private_addresses(self):
        def resolve(host, port, proto=0):
            if host.startswith("mvp01-sql"):
                raise socket.gaierror("no such host")
            address = "52.1.2.3" if "digest" in host else "10.20.3.4"
            return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (address, port))]

        problems = smoke.dns_problems(smoke.private_hosts(INFRA), resolve)
        self.assertEqual(len(problems), 2)
        self.assertIn("does not resolve", problems[0])
        self.assertIn("mvp01digest.blob.core.windows.net resolves to 52.1.2.3", problems[1])

    def test_the_audit_collects_every_problem(self):
        az = FakeAzure(cloud_routes()).on(r"Microsoft\.Sql/servers", "Enabled").on(
            r"Microsoft\.App/managedEnvironments", {"access": "Enabled", "internal": True,
                                                   "subnet": f"{VNET}/subnets/apps"})
        problems = smoke.audit(az, "mvp01-rg", INFRA)
        self.assertEqual(len(problems), 2)
        self.assertIn("mvp01-sql", problems[0])
        self.assertIn("Container Apps environment", problems[1])

    def test_readiness_times_out_and_local_storage_is_refused(self):
        clock = iter(range(0, 1000, 15))
        with self.assertRaisesRegex(MvpError, "--from B7"):
            smoke.wait_ready(APP_URL, lambda _: (503, '{"status":"not_ready"}'), sleep=lambda _: None,
                             clock=lambda: next(clock), out=lambda *_: None)
        local = json.dumps({"status": "ready", "store": "memory", "blobs": "local"})
        with self.assertRaisesRegex(MvpError, "local storage"):
            smoke.wait_ready(APP_URL, lambda _: (200, local), sleep=lambda _: None, clock=lambda: 0,
                             out=lambda *_: None)

    def test_a_failed_telemetry_query_is_reported_not_raised(self):
        az = FakeAzure([(r"applicationinsights", Seq(Fail("ERROR: (PathNotFound) The application was not found."),
                                                     TELEMETRY_FOUND))])
        printed, sleeps = [], []
        self.assertTrue(smoke.agent_telemetry(az, INSIGHTS, sleep=sleeps.append, clock=lambda: 0,
                                              out=printed.append))
        self.assertIn("Application Insights query failed (ERROR: (PathNotFound)", printed[0])
        self.assertEqual(sleeps, [smoke.TELEMETRY_INTERVAL])
        query = json.loads(az.files[-1][2])
        self.assertEqual(query["timespan"], "P1D")
        self.assertIn('name == "agent.run"', query["query"])
        self.assertIn('== "supplier"', query["query"])


class CommandLineTests(Workspace):
    def main(self, *argv, az=None):
        printed = io.StringIO()
        with mock.patch.object(cli, "load", return_value=make_config()), \
                mock.patch.object(cli, "Azure", return_value=az or FakeAzure(cloud_routes())), \
                mock.patch.object(cli, "State", return_value=self.state), \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = cli.main(list(argv))
        return code, printed.getvalue()

    def test_deploy_options_reach_the_stage(self):
        with mock.patch.object(deploy, "run") as run:
            code, _ = self.main("deploy", "--from", "B3", "--skip-build")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs, {"start": "B3", "skip_build": True, "image": None})
        with self.assertRaises(SystemExit):
            self.main("deploy", "--skip-build", "--image", "mvp01acr.azurecr.io/passport-app:v1")

    def test_app_redeploy_dispatches_without_running_stage_b(self):
        with mock.patch.object(deploy, "app_redeploy") as redeploy, mock.patch.object(deploy, "run") as run:
            code, _ = self.main("app", "redeploy")
        self.assertEqual(code, 0)
        redeploy.assert_called_once()
        self.assertIs(redeploy.call_args.args[2], self.state)
        run.assert_not_called()
        with mock.patch.object(deploy, "app_redeploy", side_effect=MvpError("image build failed")):
            code, printed = self.main("app", "redeploy")
        self.assertEqual(code, 1)
        self.assertIn("image build failed", printed)
        with mock.patch.object(deploy, "app_redeploy", side_effect=KeyboardInterrupt):
            code, printed = self.main("app", "redeploy")
        self.assertEqual(code, 130)
        self.assertIn("build and deploy a fresh image", printed)
        self.assertNotIn("finished phases are skipped", printed)

    def test_app_prices_accepts_a_file_without_loading_stage_state_or_running_stage_b(self):
        for arguments, path in (([], None), (["--file", "config/prices.json"], "config/prices.json")):
            with self.subTest(arguments=arguments), \
                    mock.patch.object(cli, "load", return_value=make_config()) as load, \
                    mock.patch.object(cli, "Azure") as azure, \
                    mock.patch.object(cli, "State", side_effect=AssertionError("No Stage B state needed")), \
                    mock.patch.object(deploy, "app_prices") as prices, \
                    mock.patch.object(deploy, "app_redeploy") as redeploy, \
                    mock.patch.object(deploy, "run") as run:
                self.assertEqual(cli.main(["app", "prices", *arguments]), 0)
                load.assert_called_once_with(str(cli.CONFIG), prices_file=path)
                prices.assert_called_once_with(load.return_value, azure.return_value)
                redeploy.assert_not_called()
                run.assert_not_called()
        with self.assertRaises(SystemExit):
            self.main("app", "redeploy", "--file", "config/prices.json")

    def test_app_prices_errors_and_interrupts_do_not_suggest_rebuilding(self):
        with mock.patch.object(deploy, "app_prices", side_effect=MvpError("price update refused")):
            code, printed = self.main("app", "prices")
        self.assertEqual(code, 1)
        self.assertIn("price update refused", printed)
        with mock.patch.object(deploy, "app_prices", side_effect=KeyboardInterrupt):
            code, printed = self.main("app", "prices", "--file", "config/prices.json")
        self.assertEqual(code, 130)
        self.assertIn("same app prices command", printed)
        self.assertNotIn("fresh image", printed)

    def test_ledger_command_dispatches(self):
        with mock.patch.object(deploy, "ledger") as ledger:
            code, _ = self.main("ledger", "verify")
        self.assertEqual((code, ledger.call_args.args[2]), (0, "verify"))

    def test_status_lists_both_stages(self):
        az = FakeAzure(session_routes() + [(r"^group exists", False)])
        code, printed = self.main("status", az=az)
        self.assertEqual(code, 0)
        for phase in ("A0", "A3", "B1", "B4", "B8"):
            self.assertIn(f"  {phase}  not done", printed)

    def test_roles_command_assigns_to_a_named_user(self):
        az = FakeAzure(graph_routes() + [(rf"--method get --url {GRAPH}/users/tester ",
                                          {"id": OPERATOR, "userPrincipalName": "tester"})])
        with mock.patch.object(deploy.preflight, "session_check"):
            self.state.record("B1", "fp", {"service_principal_id": SP})
            code, printed = self.main("roles", "assign", "Passport.OEM", "Passport.Authority", "--user", "tester",
                                      az=az)
            self.assertEqual(code, 0, printed)
            self.assertIn("Assigned Passport.OEM, Passport.Authority to tester.", printed)
            self.assertIn("tester holds: Passport.Authority, Passport.OEM.", printed)
            code, printed = self.main("roles", "assign", "Passport.Owner", az=az)
            self.assertEqual(code, 1)
            self.assertIn("Passport.Owner is not a role of this MVP", printed)
            code, printed = self.main("roles", "list", "Passport.OEM", az=az)
            self.assertIn("roles list takes no role names", printed)

    def test_scenario_command_dispatches(self):
        with mock.patch.object(deploy, "evaluation") as evaluation:
            code, _ = self.main("scenario", "evaluation", "--apply", "--repeats", "3")
        self.assertEqual((code, evaluation.call_args.kwargs), (0, {"apply": True, "repeats": 3}))
        with self.assertRaises(SystemExit):
            self.main("scenario", "evaluation", "--repeats", "9")
        with mock.patch.object(deploy, "credential_negatives") as negatives, mock.patch.object(cli, "load") as load:
            self.assertEqual(self.main("scenario", "credential_negatives")[0], 0)
            code, printed = self.main("scenario", "credential_negatives", "--repeats", "2")
        self.assertEqual(negatives.call_args.args[:3], (None, None, None))
        self.assertFalse(load.called)
        self.assertEqual(code, 1)
        self.assertIn("--repeats belongs to scenario evaluation.", printed)


if __name__ == "__main__":
    unittest.main()
