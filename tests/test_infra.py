"""Compile the MVP Bicep locally and check its safety and contract properties.

Needs the Azure CLI (az bicep) or the Bicep CLI, plus the app requirements for the contract checks.
No Azure sign-in and no deployment: a template that compiles has not been deployed.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
MODULES = INFRA / "modules"
APP = ROOT / "app"
sys.path.insert(0, str(APP))

SAMPLE_ENV = {
    "DBP_PREFIX": "mvp01",
    "DBP_OPERATOR_UPN": "operator@contoso.example",
    "DBP_OPERATOR_OBJECT_ID": "00000000-0000-0000-0000-000000000001",
    "DBP_CLIENT_IP": "203.0.113.10",
    "DBP_VM_PASSWORD": "Sample-Only-Passw0rd!",
}
ACCOUNT = "Microsoft.CognitiveServices/accounts"


def _tool():
    if shutil.which("az"):
        return [shutil.which("az"), "bicep"], True
    if shutil.which("bicep"):
        return [shutil.which("bicep")], False
    raise unittest.SkipTest("Neither the Azure CLI nor the Bicep CLI is installed.")


def _run(command, path, extra_env=None):
    tool, is_az = _tool()
    args = tool + [command] + (["--file", str(path)] if is_az else [str(path)]) + ["--stdout"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(extra_env or {})}
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    if result.returncode:
        raise AssertionError(f"{command} {path.name} failed:\n{result.stderr}")
    return json.loads(result.stdout), result.stderr


@functools.cache
def build(path):
    return _run("build", path)


def compiled(path):
    return build(path)[0]


def resources(template):
    found = template["resources"]
    return list(found.values()) if isinstance(found, dict) else found


def one(template, resource_type, name=None):
    matches = [r for r in resources(template) if r["type"] == resource_type and (name is None or r["name"] == name)]
    if len(matches) != 1:
        raise AssertionError(f"expected one {resource_type} {name or ''}, found {len(matches)}")
    return matches[0]


class CompileTests(unittest.TestCase):
    def test_every_template_compiles_without_warnings(self):
        for path in [INFRA / "main.bicep", *sorted(MODULES.glob("*.bicep"))]:
            with self.subTest(template=path.name):
                _, diagnostics = build(path)
                findings = [line for line in diagnostics.splitlines()
                            if re.search(r"\b(Warning|Error)\b", line) and "upgrade" not in line]
                self.assertEqual(findings, [])

    def test_parameter_file_reads_only_required_values_from_the_environment(self):
        values, _ = _run("build-params", INFRA / "main.bicepparam", SAMPLE_ENV)
        parameters = json.loads(values["parametersJson"])["parameters"] if "parametersJson" in values \
            else values["parameters"]
        self.assertEqual(set(parameters), {"prefix", "location", "operatorUpn", "operatorObjectId",
                                           "clientIp", "vmAdminPassword"})
        self.assertEqual(parameters["location"]["value"], "swedencentral")
        required = {name for name, spec in compiled(INFRA / "main.bicep")["parameters"].items()
                    if "defaultValue" not in spec}
        self.assertLessEqual(required, set(parameters))
        self.assertNotIn(SAMPLE_ENV["DBP_VM_PASSWORD"], (INFRA / "main.bicepparam").read_text(encoding="utf-8"))


class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = compiled(INFRA / "main.bicep")

    def test_subscription_entry_point_and_cost_safe_defaults(self):
        self.assertIn("subscriptionDeploymentTemplate", self.main["$schema"])
        defaults = {name: spec.get("defaultValue") for name, spec in self.main["parameters"].items()}
        self.assertEqual(defaults["location"], "swedencentral")
        self.assertEqual(defaults["vmAccess"], "source-ip")
        self.assertEqual(defaults["fabricSku"], "F2")
        self.assertFalse(defaults["enableAlerts"])
        self.assertFalse(defaults["enableNatGateway"])
        password = self.main["parameters"]["vmAdminPassword"]
        self.assertEqual((password["type"], password["minLength"]), ("securestring", 12))
        self.assertNotIn("defaultValue", password)
        modules = {r["name"]: r.get("condition") for r in resources(self.main)
                   if r["type"] == "Microsoft.Resources/deployments"}
        self.assertEqual(modules, {"nat": "[parameters('enableNatGateway')]", "network": None, "devbox": None,
                                   "platform": None, "monitoring": None, "foundry": None,
                                   "fabric-capacity": "[parameters('deployFabric')]"})

    def test_default_models_cover_every_agent_contract_model(self):
        from passport.agent_contracts import ROLES

        models = self.main["parameters"]["models"]["defaultValue"]
        deployed = [(m["model"]["name"], m["model"]["version"]) for m in models]
        self.assertEqual(len(deployed), len(set(deployed)))
        self.assertEqual(set(deployed), {(role["model"], role["version"]) for role in ROLES.values()})
        for model in models:
            self.assertEqual(model["name"], model["model"]["name"])

    def test_app_parameters_output_feeds_the_app_template(self):
        app = compiled(MODULES / "app.bicep")["parameters"]
        handed_over = set(self.main["outputs"]["appParameters"]["value"])
        self.assertLessEqual(handed_over, set(app))
        still_required = {name for name, spec in app.items() if "defaultValue" not in spec} - handed_over
        self.assertEqual(still_required, {"image", "appClientId", "appClientSecret"})

    def test_stage_b_finds_every_output_it_reads(self):
        from dbp_mvp import deploy

        self.assertLessEqual(set(deploy.REQUIRED), set(self.main["outputs"]))


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = compiled(MODULES / "network.bicep")

    def test_rdp_rule_depends_on_access_mode(self):
        variables = self.template["variables"]
        allow, deny = variables["rdpFromClient"]["properties"], variables["denyAllInbound"]["properties"]
        self.assertEqual((allow["access"], allow["priority"], allow["destinationPortRange"]), ("Allow", 100, "3389"))
        self.assertEqual(allow["sourceAddressPrefix"], "[format('{0}/32', parameters('clientIp'))]")
        self.assertEqual((deny["access"], deny["priority"]), ("Deny", 4096))
        nsg = one(self.template, "Microsoft.Network/networkSecurityGroups")
        self.assertEqual(nsg["properties"]["securityRules"],
                         "[if(equals(parameters('vmAccess'), 'source-ip'), createArray(variables('rdpFromClient'), "
                         "variables('denyAllInbound')), createArray(variables('denyAllInbound')))]")

    def test_subnets_and_private_dns(self):
        vnet = one(self.template, "Microsoft.Network/virtualNetworks")
        subnets = {s["name"]: s["properties"] for s in vnet["properties"]["subnets"]}
        self.assertEqual({name: s["addressPrefix"] for name, s in subnets.items()},
                         {"apps": "10.64.0.0/24", "endpoints": "10.64.1.0/24", "devbox": "10.64.2.0/27",
                          "agents": "10.64.4.0/24"})
        for name in ("apps", "agents"):
            self.assertEqual(subnets[name]["delegations"][0]["properties"]["serviceName"], "Microsoft.App/environments")
        for name in ("endpoints", "devbox"):
            self.assertFalse(subnets[name]["defaultOutboundAccess"])
        # Hosted agent builds need outbound access: Azure default outbound unless the NAT gateway is enabled.
        self.assertEqual(subnets["agents"]["defaultOutboundAccess"], "[empty(parameters('natGatewayId'))]")
        self.assertIn("networkSecurityGroup", subnets["devbox"])
        self.assertEqual(len(self.template["variables"]["zoneNames"]), 5)

    def test_private_dns_links_are_serial_and_wait_for_the_zone_and_vnet(self):
        links = one(self.template, "Microsoft.Network/privateDnsZones/virtualNetworkLinks")
        self.assertEqual(links["copy"]["mode"], "serial")
        self.assertEqual(links["copy"]["batchSize"], 1)
        self.assertEqual(links["copy"]["count"], "[length(items(variables('zoneNames')))]")
        self.assertIn("[resourceId('Microsoft.Network/virtualNetworks', format('{0}-vnet', parameters('prefix')))]",
                      links["dependsOn"])
        self.assertTrue(any("Microsoft.Network/privateDnsZones" in dependency
                            and "copyIndex()" in dependency for dependency in links["dependsOn"]))
        self.assertFalse(links["properties"]["registrationEnabled"])


class DevboxTests(unittest.TestCase):
    def test_vm_is_hardened_scheduled_and_jit_is_optional(self):
        template = compiled(MODULES / "devbox.bicep")
        self.assertEqual(template["parameters"]["adminPassword"]["type"], "securestring")
        machine = one(template, "Microsoft.Compute/virtualMachines")
        # Reruns skip the VM: ARM cannot change an existing VM's password, so it must not be redeployed.
        self.assertEqual(machine["condition"], "[parameters('createVm')]")
        vm = machine["properties"]
        self.assertEqual(vm["securityProfile"]["securityType"], "TrustedLaunch")
        self.assertEqual(vm["storageProfile"]["imageReference"]["offer"], "windows-11")
        target = "[resourceId('Microsoft.Compute/virtualMachines', variables('vmName'))]"
        schedule = one(template, "Microsoft.DevTestLab/schedules")["properties"]
        self.assertEqual((schedule["status"], schedule["taskType"]), ("Enabled", "ComputeVmShutdownTask"))
        self.assertEqual(schedule["targetResourceId"], target)
        jit = one(template, "Microsoft.Security/locations/jitNetworkAccessPolicies")
        self.assertEqual(jit["condition"], "[equals(parameters('vmAccess'), 'jit')]")
        self.assertEqual(jit["properties"]["virtualMachines"][0]["id"], target)
        self.assertEqual(jit["properties"]["virtualMachines"][0]["ports"][0]["number"], 3389)
        self.assertEqual(template["variables"]["vmName"], "[format('{0}-devbox', parameters('prefix'))]")


class OrchestratorParityTests(unittest.TestCase):
    """The orchestrator (dbp_mvp) and the templates must agree on names, defaults and parameters."""

    def test_constants_and_parameters_match_the_templates(self):
        sys.path.insert(0, str(ROOT))
        from dbp_mvp import config

        main = compiled(INFRA / "main.bicep")
        parameters = main["parameters"]
        bicep_models = parameters["models"]["defaultValue"]
        self.assertEqual([{"name": m["name"], "version": m["model"]["version"], "sku": m["sku"]["name"],
                           "capacity": m["sku"]["capacity"]} for m in bicep_models],
                         [dict(m) for m in config.DEFAULT_MODELS])
        self.assertEqual(parameters["vmSize"]["defaultValue"], config.VM_SIZE)
        self.assertEqual(parameters["vmAdminUsername"]["defaultValue"], config.VM_ADMIN)
        self.assertEqual(main["variables"]["tags"]["project"], config.PROJECT_TAG)
        image = one(compiled(MODULES / "devbox.bicep"), "Microsoft.Compute/virtualMachines")["properties"][
            "storageProfile"]["imageReference"]
        self.assertEqual(":".join(image[key] for key in ("publisher", "offer", "sku", "version")), config.VM_IMAGE)

        sample = config.parse({"subscription_id": "00000000-0000-0000-0000-000000000001",
                               "operator_upn": "operator@contoso.example", "prefix": "mvp01",
                               "client_ip": "8.8.8.8"})
        values = sample.bicep_parameters("00000000-0000-0000-0000-000000000002", "unused", True)
        self.assertLessEqual(set(values), set(parameters))
        required = {name for name, spec in parameters.items() if "defaultValue" not in spec}
        self.assertLessEqual(required, set(values))
        self.assertEqual(values["models"], bicep_models)
        network = compiled(MODULES / "network.bicep")
        names = sample.names
        self.assertEqual(network["variables"]["rdpFromClient"]["name"], names.rdp_rule)
        nsg = one(network, "Microsoft.Network/networkSecurityGroups")["name"]
        self.assertEqual(nsg, "[format('{0}-devbox-nsg', parameters('prefix'))]")
        self.assertEqual(one(network, "Microsoft.Network/virtualNetworks")["name"],
                         "[format('{0}-vnet', parameters('prefix'))]")
        self.assertEqual((names.nsg, names.vnet, names.vm), ("mvp01-devbox-nsg", "mvp01-vnet", "mvp01-devbox"))

    def test_direct_rest_calls_use_the_template_api_versions_and_no_previews(self):
        sys.path.insert(0, str(ROOT))
        from dbp_mvp import deploy, devbox, fabric, teardown

        def version(template, resource_type):
            return one(compiled(MODULES / template), resource_type)["apiVersion"]

        self.assertEqual(teardown.FOUNDRY_API, version("foundry.bicep", f"{ACCOUNT}/projects/capabilityHosts"))
        self.assertEqual(fabric.CAPACITY_API, version("fabric-capacity.bicep", "Microsoft.Fabric/capacities"))
        self.assertEqual(deploy.DURABLE_TASK_API,
                         version("app.bicep", "Microsoft.DurableTask/schedulers/taskHubs"))
        self.assertEqual(devbox.JIT_API,
                         version("devbox.bicep", "Microsoft.Security/locations/jitNetworkAccessPolicies"))
        # diagnosticSettings has no stable version that can target another resource (2016-09-01 is the classic form).
        allowed = ["foundry.bicep: @2021-05-01-preview'", "foundry.bicep: @2021-05-01-preview'",
                   "platform.bicep: @2021-05-01-preview'"]
        previews = [f"{path.name}: {match}" for path in INFRA.rglob("*.bicep")
                    for match in re.findall(r"@\d{4}-\d{2}-\d{2}-preview'", path.read_text(encoding="utf-8"))]
        self.assertEqual(sorted(previews), sorted(allowed))
        for template in ("foundry.bicep", "platform.bicep"):
            self.assertIn("Microsoft.Insights/diagnosticSettings@2021-05-01-preview'",
                          (MODULES / template).read_text(encoding="utf-8"))


class PlatformTests(unittest.TestCase):
    def test_database_requests_paid_gen5_serverless_compute(self):
        database = one(compiled(MODULES / "platform.bicep"), "Microsoft.Sql/servers/databases")
        self.assertEqual(database["sku"], {"name": "GP_S_Gen5_2", "tier": "GeneralPurpose",
                                           "family": "Gen5", "capacity": 2})
        properties = database["properties"]
        self.assertIs(properties["useFreeLimit"], False)
        self.assertNotIn("freeLimitExhaustionBehavior", properties)
        self.assertEqual((properties["minCapacity"], properties["autoPauseDelay"]), ("[json('0.5')]", 60))

    def test_data_stores_are_private_and_keyless(self):
        template = compiled(MODULES / "platform.bicep")
        accounts = [r for r in resources(template) if r["type"] == "Microsoft.Storage/storageAccounts"]
        self.assertEqual(len(accounts), 2)
        for account in accounts:
            self.assertFalse(account["properties"]["allowSharedKeyAccess"])
            self.assertEqual(account["properties"]["publicNetworkAccess"], "Disabled")
        sql = one(template, "Microsoft.Sql/servers")["properties"]
        self.assertEqual(sql["publicNetworkAccess"], "Disabled")
        self.assertTrue(sql["administrators"]["azureADOnlyAuthentication"])
        policy = one(template, "Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies")
        self.assertNotIn("state", policy["properties"])  # unlocked, so teardown can delete it
        self.assertEqual(len(template["variables"]["endpointSpecs"]), 3)

    def test_sql_security_audit_goes_to_log_analytics(self):
        template = compiled(MODULES / "platform.bicep")
        audit = one(template, "Microsoft.Sql/servers/auditingSettings")
        self.assertEqual((audit["properties"]["state"], audit["properties"]["isAzureMonitorTargetEnabled"]),
                         ("Enabled", True))
        self.assertIn("FAILED_DATABASE_AUTHENTICATION_GROUP", audit["properties"]["auditActionsAndGroups"])
        self.assertNotIn("BATCH_COMPLETED_GROUP", audit["properties"]["auditActionsAndGroups"])
        setting = one(template, "Microsoft.Insights/diagnosticSettings")
        self.assertIn("'master')", setting["scope"])
        self.assertEqual(setting["properties"]["logs"], [{"category": "SQLSecurityAuditEvents", "enabled": True}])
        self.assertTrue(any("diagnosticSettings" in dependency for dependency in audit["dependsOn"]))


class FoundryTests(unittest.TestCase):
    def test_account_writes_are_serialised_and_project_is_traced(self):
        template = compiled(MODULES / "foundry.bicep")
        account = one(template, ACCOUNT)
        self.assertEqual(account["properties"]["publicNetworkAccess"], "Disabled")
        self.assertTrue(account["properties"]["disableLocalAuth"])
        models = one(template, ACCOUNT + "/deployments")
        self.assertEqual((models["copy"]["mode"], models["copy"]["batchSize"]), ("serial", 1))
        endpoint = one(template, "Microsoft.Resources/deployments", "foundry-endpoint")
        self.assertIn(models["copy"]["name"], endpoint["dependsOn"])
        project = one(template, ACCOUNT + "/projects")
        self.assertIn("[resourceId('Microsoft.Resources/deployments', 'foundry-endpoint')]", project["dependsOn"])
        host = one(template, ACCOUNT + "/projects/capabilityHosts")
        self.assertTrue(any(f"'{ACCOUNT}/projects'" in d for d in host["dependsOn"]))
        tracing = one(template, ACCOUNT + "/projects/connections")
        self.assertEqual(tracing["properties"]["category"], "AppInsights")
        self.assertTrue(any("capabilityHosts" in d for d in tracing["dependsOn"]))
        roles = template["variables"]["roles"]
        self.assertEqual(roles, {"foundryProjectManager": "eadc314b-1a2d-4efa-be10-5d325db5065e",
                                 "foundryUser": "53ca6127-db72-4b80-b1b0-d745d6d5456d",
                                 "cognitiveServicesUser": "a97b65f3-24c7-4388-baec-2e87135dc908"})
        grants = sorted((r["properties"]["principalId"], r["properties"]["principalType"],
                         r["properties"]["roleDefinitionId"].split("roles').")[1].rstrip(")]"))
                        for r in resources(template) if r["type"] == "Microsoft.Authorization/roleAssignments")
        self.assertEqual(grants, [
            ("[parameters('appPrincipalId')]", "ServicePrincipal", "cognitiveServicesUser"),
            ("[parameters('appPrincipalId')]", "ServicePrincipal", "foundryUser"),
            ("[parameters('operatorObjectId')]", "User", "cognitiveServicesUser"),
            ("[parameters('operatorObjectId')]", "User", "foundryProjectManager")])

    def test_account_and_project_logs_go_to_the_app_workspace(self):
        template = compiled(MODULES / "foundry.bicep")
        settings = [r for r in resources(template) if r["type"] == "Microsoft.Insights/diagnosticSettings"]
        self.assertEqual(len(settings), 2)
        account_setting = next(setting for setting in settings if "accounts/projects" not in setting["scope"])
        project_setting = next(setting for setting in settings if "accounts/projects" in setting["scope"])
        for setting in (account_setting, project_setting):
            self.assertEqual(setting["name"], "foundry-logs")
            self.assertIn("parameters('workspaceName')", setting["properties"]["workspaceId"])
            self.assertEqual(setting["properties"]["logs"], [{"categoryGroup": "allLogs", "enabled": True}])
            self.assertEqual(setting["properties"]["metrics"], [{"category": "AllMetrics", "enabled": True}])
        self.assertIn("accounts/projects", project_setting["scope"])
        self.assertIn("workspaceName", (INFRA / "main.bicep").read_text(encoding="utf-8").split("module foundry")[1])


class MonitoringTests(unittest.TestCase):
    def test_alerts_stay_disabled_until_enabled(self):
        template = compiled(MODULES / "monitoring.bicep")
        rules = one(template, "Microsoft.Insights/scheduledQueryRules")
        self.assertEqual(rules["properties"]["enabled"], "[parameters('enableAlerts')]")
        self.assertEqual(one(template, "Microsoft.Insights/actionGroups")["condition"], "[parameters('enableAlerts')]")
        aged = next(alert for alert in template["variables"]["alerts"] if alert["key"] == "aged-hold")
        self.assertRegex(aged["query"], r"oldest_hold_minutes\) > __HOLD_AGE_MINUTES__\n\s+or todouble\("
                                        r"customMeasurements\.oldest_case_minutes\) > __HOLD_AGE_MINUTES__")
        stuck = next(alert for alert in template["variables"]["alerts"] if alert["key"] == "stuck-work")
        self.assertIn("customMeasurements.stuck_batteries", stuck["query"])

    def test_workbook_queries_use_only_events_and_fields_of_the_telemetry_contract(self):
        from passport import telemetry

        allowed = {"dbp.schema_version", "event_id", *telemetry.COMMON}
        used = set()
        for file in sorted((INFRA / "workbooks").glob("*.workbook.json")):
            workbook = json.loads(file.read_text(encoding="utf-8"))
            for query in (item["content"]["query"] for item in workbook["items"] if item["type"] == 3):
                if 'cloud_RoleName == "passport-app"' not in query or not query.startswith("customEvents"):
                    continue
                names = set(re.findall(r'name == "([^"]+)"', query))
                for listed in re.findall(r"name in \(([^)]*)\)", query):
                    names |= set(re.findall(r'"([^"]+)"', listed))
                with self.subTest(file=file.name, events=sorted(names)):
                    self.assertTrue(names and names <= set(telemetry.EVENTS))
                    fields = allowed | {field for name in names for group in telemetry.EVENTS[name] for field in group}
                    referenced = set(re.findall(r'custom(?:Dimensions|Measurements)(?:\.([\w]+)|\["([^"]+)"\])', query))
                    self.assertEqual({a or b for a, b in referenced} - fields, set())
                    used |= names
        self.assertLessEqual({"passport.series.snapshot", "passport.plant.snapshot", "view.served", "connector.message",
                              "lifecycle.event", "compliance.case.opened", "compliance.case.decided",
                              "compliance.case.reopened", "access.denied", "decision.recorded"}, used)

    def test_all_loaded_workbooks_resolve_resource_and_time_bindings(self):
        source = (MODULES / "monitoring.bicep").read_text(encoding="utf-8")
        loaded = {(MODULES / file).resolve() for file in re.findall(r"loadTextContent\('([^']+)'\)", source)}
        files = {file.resolve() for file in (INFRA / "workbooks").glob("*.workbook.json")}
        self.assertEqual(loaded, files)
        component = "/subscriptions/test/resourceGroups/mvp/providers/Microsoft.Insights/components/mvp"
        for file in files:
            with self.subTest(file=file.name):
                text = file.read_text(encoding="utf-8").replace("__APPLICATION_INSIGHTS_ID__", component)
                workbook = json.loads(text)
                self.assertNotRegex(text, r"__[A-Z_]+__")
                self.assertEqual(workbook["fallbackResourceIds"], [component])
                parameters = {p["name"] for item in workbook["items"] if item["type"] == 9
                              for p in item["content"]["parameters"]}
                queries = [item["content"] for item in workbook["items"] if item["type"] == 3]
                self.assertGreater(len(queries), 0)
                for query in queries:
                    self.assertEqual(query["crossComponentResources"], [component])
                    self.assertIn(query["timeContextFromParameter"], parameters)
                    self.assertIn("timestamp {TimeRange}", query["query"])
                    self.assertNotRegex(query["query"],
                                        r"gen_ai\.(?:input|output)\.messages|tool\.call\.(?:arguments|result)")


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = compiled(MODULES / "app.bicep")
        cls.app = one(cls.template, "Microsoft.App/containerApps")

    def test_consumption_scheduler_omits_provisioned_capacity(self):
        scheduler = one(self.template, "Microsoft.DurableTask/schedulers")
        self.assertEqual(scheduler["properties"]["sku"], {"name": "Consumption"})

    def test_scheduler_is_reachable_only_through_its_private_endpoint(self):
        scheduler = one(self.template, "Microsoft.DurableTask/schedulers")["properties"]
        self.assertEqual((scheduler["publicNetworkAccess"], scheduler["ipAllowlist"]), ("Disabled", []))
        zone = one(self.template, "Microsoft.Network/privateDnsZones")
        self.assertEqual((zone["name"], zone["condition"]), ("privatelink.durabletask.io", "[parameters('agentMode')]"))
        endpoint = one(self.template, "Microsoft.Resources/deployments", "scheduler-endpoint")
        self.assertEqual(endpoint["condition"], "[parameters('agentMode')]")
        self.assertEqual(endpoint["properties"]["parameters"]["groupId"], {"value": "scheduler"})
        self.assertIn("subnets/endpoints", endpoint["properties"]["parameters"]["subnetId"]["value"])
        self.assertEqual(self.template["parameters"]["vnetName"]["defaultValue"],
                         "[format('{0}-vnet', parameters('prefix'))]")
        self.assertTrue(any("scheduler-endpoint" in dependency for dependency in self.app["dependsOn"]))

    def test_sign_in_allowlist_and_secrets(self):
        auth = one(self.template, "Microsoft.App/containerApps/authConfigs")
        policy = auth["properties"]["identityProviders"]["azureActiveDirectory"]["validation"]
        self.assertEqual(policy["defaultAuthorizationPolicy"]["allowedApplications"],
                         "[concat(createArray(parameters('appClientId'), variables('azureCliAppId')), "
                         "parameters('extraAllowedClientIds'))]")
        self.assertEqual(self.template["variables"]["azureCliAppId"], "04b07795-8ddb-461a-bbee-02f9e1bf7b46")
        self.assertEqual(self.template["parameters"]["appClientSecret"]["type"], "securestring")
        secrets = {s["name"] for s in self.app["properties"]["configuration"]["secrets"]}
        self.assertEqual(secrets, {"microsoft-provider-authentication-secret", "appinsights-connection-string"})
        env = self.app["properties"]["template"]["containers"][0]["env"]
        self.assertIn("createObject('name', 'APPLICATIONINSIGHTS_CONNECTION_STRING', 'secretRef', "
                      "'appinsights-connection-string')", env)

    def test_passport_container_sizing_and_scale_bounds(self):
        template = self.app["properties"]["template"]
        passport = next(c for c in template["containers"] if c["name"] == "passport")
        self.assertEqual(passport["resources"], {"cpu": 1, "memory": "2Gi"})
        self.assertEqual(template["scale"]["maxReplicas"], 3)
        self.assertEqual(template["scale"]["minReplicas"],
                         "[if(or(parameters('warmReplica'), parameters('agentMode')), 1, 0)]")

    def test_every_setting_is_read_by_the_app(self):
        from passport import config

        source = "\n".join(path.read_text(encoding="utf-8") for path in (APP / "passport").glob("*.py"))
        env = self.app["properties"]["template"]["containers"][0]["env"]
        names = set(re.findall(r"createObject\('name', '([A-Z0-9_]+)'", env))
        names |= {item["name"] for item in self.template["variables"]["optionalEnv"]}
        self.assertGreater(len(names), 15)
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(source, rf"[\"']{name}[\"']")
        personas = re.search(r"for persona in \[([^\]]+)\]", (MODULES / "app.bicep").read_text(encoding="utf-8"))
        keys = re.findall(r"'([a-z]+)'", personas.group(1))
        self.assertEqual([(key, f"DBP_WORKBOOK_{key.upper()}") for key in keys],
                         [(entry[0], entry[1]) for entry in config.WORKBOOKS])


class ImageTests(unittest.TestCase):
    def test_image_installs_only_the_hash_locked_direct_pins_on_a_pinned_base(self):
        dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^FROM python:3\.14-slim@sha256:[0-9a-f]{64}\s*$")
        self.assertIn("--require-hashes", dockerfile)

        def normal(name):
            return re.sub(r"[-_.]+", "-", name).lower()

        lock = (APP / "requirements-lock.txt").read_text(encoding="utf-8")
        locked = {normal(name): version for name, version in re.findall(r"(?m)^([A-Za-z0-9._-]+)==([^\s\\]+)", lock)}
        pins = [line.split("==") for line in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertGreater(len(pins), 10)
        for name, version in pins:
            with self.subTest(package=name):
                self.assertEqual(locked.get(normal(name)), version.strip(),
                                 "Regenerate requirements-lock.txt (app/README.md, section 8.1).")


class HygieneTests(unittest.TestCase):
    """This folder is meant to be published on its own: no legacy names, secrets, real identifiers or broken links."""

    # Bracketed letters keep this file free of the words and patterns it looks for.
    FORBIDDEN = re.compile(r"\bd[e]mo(?:\d|\b)|dbp5[p]|\bd4[a]_|\bl[a]bs?\b|l[a]badmin|j[u]mp|SIM-[0]|c[a]pstone"
                           r"|c[o]horts?|r[e]hears|wh[i]tepaper|r[o]ute-a\b|\bt[e]aching\b|c-l[e]vel|pr[o]jector"
                           r"|l[e]arning|pr[e]sentation|\bsl[i]des?\b", re.IGNORECASE)
    UPPER_CASE = re.compile(r"\bP[A]NEL\b")
    SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AccountK[e]y=|SharedAccessK[e]y|[?&]sig="
                        r"|ghp_[A-Za-z0-9]{20,}|github_p[a]t_")
    GUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
    # Published Microsoft IDs: Azure CLI, Microsoft Graph and its sign-in scopes, Entra role templates, Azure roles.
    PUBLIC_IDS = {
        "04b07795-8ddb-461a-bbee-02f9e1bf7b46", "00000003-0000-0000-c000-000000000000",
        "37f7f235-527c-4136-accd-4a02d197296e", "14dad69e-099b-42c9-810b-d002981feec1",
        "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0", "62e90394-69f5-4237-9190-012177145e10",
        "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3", "158c047a-c907-4556-b7ef-446551a6b5f7",
        "cf1c38e5-3621-4004-a7cb-879624dced7c", "7f951dda-4ed3-4680-a7ca-43fe172d538d",
        "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",
        "eadc314b-1a2d-4efa-be10-5d325db5065e", "53ca6127-db72-4b80-b1b0-d745d6d5456d",
        "0ad04412-c4d5-4796-b79c-f76d14c8d402", "a97b65f3-24c7-4388-baec-2e87135dc908",
    }
    EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
    SAMPLE_DOMAIN = re.compile(r"(?:contoso\.com|example\.(?:com|org|net)|[\w.-]+\.example)$", re.IGNORECASE)
    LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    PRUNED = {".git", ".venv", ".azure", ".ruff_cache", ".pytest_cache", "__pycache__", "output"}

    @classmethod
    def files(cls):
        for top, folders, names in os.walk(ROOT):
            folders[:] = sorted(name for name in folders if name not in cls.PRUNED)
            for name in sorted(set(names) - cls.PRUNED):
                path = Path(top) / name
                if path.relative_to(ROOT).as_posix() in ("config/mvp.toml", "config/prices.json"):
                    continue
                try:
                    yield path, path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue

    @staticmethod
    def located(relative, text, match):
        return f"{relative}:{text.count('\n', 0, match.start()) + 1}: {match.group()}"

    @staticmethod
    def placeholder(guid):
        digits = guid.replace("-", "")
        return digits.count("0") >= 20 or len(set(digits[:8])) == 1

    @staticmethod
    def prose(text):
        return re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", text, flags=re.MULTILINE | re.DOTALL)

    @classmethod
    def anchors(cls, path):
        """GitHub's heading anchors: lower case, punctuation removed, spaces as hyphens, repeats numbered."""
        seen = {}
        for line in cls.prose(path.read_text(encoding="utf-8")).splitlines():
            heading = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
            if heading:
                title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(1))
                slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
                seen[slug] = seen.get(slug, -1) + 1
                yield slug if not seen[slug] else f"{slug}-{seen[slug]}"

    def test_no_legacy_names_remain(self):
        found = []
        for _, relative, text in self.files():
            for pattern in (self.FORBIDDEN, self.UPPER_CASE):
                found += [self.located(relative, text, match) for match in pattern.finditer(text)]
        self.assertEqual(found, [])

    def test_no_secrets_or_real_identifiers(self):
        found = []
        for _, relative, text in self.files():
            found += [self.located(relative, text, match) for match in self.SECRET.finditer(text)]
            found += [self.located(relative, text, match) for match in self.GUID.finditer(text)
                      if match.group().lower() not in self.PUBLIC_IDS and not self.placeholder(match.group())]
            found += [self.located(relative, text, match) for match in self.EMAIL.finditer(text)
                      if not self.SAMPLE_DOMAIN.search(match.group(1)) and match.group() != "git@github.com"
                      and not re.search(r"://[^\s\"'<>]*$", text[max(match.start() - 200, 0):match.start()])]
        self.assertEqual(found, [])

    def test_markdown_links_resolve_inside_this_folder(self):
        broken = []
        for path, relative, text in self.files():
            if path.suffix != ".md":
                continue
            for target in self.LINK.findall(self.prose(text)):
                if re.match(r"(?:https?|mailto):", target):
                    continue
                file, _, anchor = target.partition("#")
                destination = (path.parent / file).resolve() if file else path
                if not destination.is_relative_to(ROOT) or not destination.exists():
                    broken.append(f"{relative}: {target}")
                elif anchor and destination.suffix == ".md" and anchor not in set(self.anchors(destination)):
                    broken.append(f"{relative}: {target} (no such heading)")
        self.assertEqual(broken, [])

    def test_the_checks_catch_what_they_look_for(self):
        # Split literals, so that this file does not trip its own scan.
        self.assertTrue(self.FORBIDDEN.search("see the l" "ab guide"))
        self.assertTrue(self.SECRET.search("AccountK" "ey=abc"))
        guid = "12345678-9abc-4def-" "8123-456789abcdef"
        self.assertFalse(self.placeholder(guid) or guid in self.PUBLIC_IDS)
        self.assertFalse(self.SAMPLE_DOMAIN.search(self.EMAIL.search("ops@fabrikam" ".com").group(1)))
        headings = set(self.anchors(ROOT / "README.md"))
        self.assertLessEqual({"scenario-at-a-glance", "costs-drivers-and-controls"}, headings)


if __name__ == "__main__":
    unittest.main()
