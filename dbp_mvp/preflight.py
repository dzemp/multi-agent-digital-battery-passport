"""Checks before anything is created: tools, sign-in, permissions, providers, region capacity and existing resources.

Decisions live in small pure functions so tests can exercise them without Azure.
"""

import re
import time

from . import MvpError
from .az import AzError
from .config import PROJECT_TAG, VM_IMAGE, VM_SIZE
from .identity import GRANT_ROLE_TEMPLATES, GRANT_ROLES
from .progress import note, stage

FABRIC_API = "https://api.fabric.microsoft.com"
# Fabric's answer when it refuses the operator rather than failing for another reason (network, throttling).
FABRIC_REFUSED = re.compile(r"\b40[13]\b|Unauthorized|Forbidden|NotLicensed|recogni[sz]ed by Microsoft Fabric|"
                            r"Sign up for Microsoft Fabric", re.IGNORECASE)
# A one-time manual step: Fabric offers no API to sign a tenant or user up.
FABRIC_ACTIVATION = (
    "Activate Fabric once, by hand: sign in to https://app.fabric.microsoft.com as operator_upn and accept the free "
    "Fabric sign-up (if self-service sign-up is blocked, an administrator assigns you a Fabric (Free) licence), and "
    "have a Fabric administrator allow 'Users can create Fabric items' "
    "(https://learn.microsoft.com/fabric/admin/fabric-switch). Or set deploy_fabric = false in config/mvp.toml; "
    "stage B then builds the synthetic package locally")
MIN_AZ = (2, 67, 0)
# Bicep release checked to know every resource API version the templates use; an older one only warns.
MIN_BICEP = (0, 49, 2)
OWNER = "Owner"
CONTRIBUTOR = "Contributor"
ACCESS_ADMINS = {"User Access Administrator", "Role Based Access Control Administrator"}
# Directory roles that may register applications even when users in general may not.
APP_ROLE_TEMPLATES = {
    "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3",  # Application Administrator
    "158c047a-c907-4556-b7ef-446551a6b5f7",  # Cloud Application Administrator
    "cf1c38e5-3621-4004-a7cb-879624dced7c",  # Application Developer
    "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
}
# Template namespaces and service-side dependencies. Register only missing providers.
# Microsoft.DevTestLab holds the VM's auto-shutdown schedule, not a development environment.
# Microsoft.AlertsManagement supports the log alert rules, which are created even when disabled.
PROVIDERS = ("Microsoft.Network", "Microsoft.Compute", "Microsoft.DevTestLab", "Microsoft.App",
             "Microsoft.CognitiveServices", "Microsoft.Sql", "Microsoft.Storage", "Microsoft.ContainerRegistry",
             "Microsoft.OperationalInsights", "Microsoft.Insights", "Microsoft.AlertsManagement",
             "Microsoft.ManagedIdentity", "Microsoft.DurableTask")
REGISTRATION_TIMEOUT = 900
REGISTRATION_POLL = 10
GRAPH = "https://graph.microsoft.com/v1.0"


class PreflightError(MvpError):
    pass


def version_tuple(text):
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3]) or (0,)


def required_providers(config):
    providers = list(PROVIDERS)
    if config.deploy_fabric:
        providers.append("Microsoft.Fabric")
    if config.vm_access == "jit":
        providers.append("Microsoft.Security")
    return providers


def missing_providers(required, registered):
    states = {item["n"].lower(): item["s"] for item in registered or []}
    return [name for name in required if states.get(name.lower()) != "Registered"]


def provider_states(az):
    return az("provider", "list", "--query", "[].{n:namespace, s:registrationState}")


def register_providers(az, missing, states, out=note, sleep=None, timeout=REGISTRATION_TIMEOUT):
    """Start every registration at once, then wait for them together: the wait is the slowest provider, not the sum.

    The preflight queries and the what-if preview need the providers registered. A provider that is already
    registering is only awaited, so a rerun after a timeout continues where Azure left off.
    """
    sleep = sleep or time.sleep
    registering = {item["n"].lower() for item in states or [] if item["s"] == "Registering"}
    for namespace in missing:
        if namespace.lower() not in registering:
            az("provider", "register", "--namespace", namespace)
    pending, waited = list(missing), 0
    while True:
        still = missing_providers(pending, provider_states(az))
        done = [name for name in pending if name not in still]
        if done:
            out(f"Registered {', '.join(done)}" + (f"; {len(still)} still registering." if still else "."))
        pending = still
        if not pending:
            return
        if waited >= timeout:
            raise PreflightError(f"Resource providers still registering after {timeout // 60} minutes: "
                                 f"{', '.join(pending)}. Azure continues in the background; rerun later. The "
                                 "Azure portal shows the state under Subscription > Resource providers.")
        sleep(REGISTRATION_POLL)
        waited += REGISTRATION_POLL


def has_required_roles(assignments, subscription_id):
    """Owner, or Contributor plus a role that can create role assignments, at subscription scope or above."""
    subscription = f"/subscriptions/{subscription_id}".lower()
    roles = set()
    for assignment in assignments or []:
        scope = (assignment.get("scope") or "").lower().rstrip("/")
        if scope in ("", subscription) or scope.startswith("/providers/microsoft.management/managementgroups/"):
            roles.add(assignment.get("role"))
    return OWNER in roles or (CONTRIBUTOR in roles and bool(roles & ACCESS_ADMINS))


def can_create_apps(policy, directory_roles):
    """True, False or None (unknown) from the authorization policy and the user's directory roles."""
    if isinstance(policy, dict) and isinstance(policy.get("value"), list):
        policy = policy["value"][0] if policy["value"] else None
    allowed = ((policy or {}).get("defaultUserRolePermissions") or {}).get("allowedToCreateApps")
    if allowed is True:
        return True
    if directory_roles is not None:
        templates = {(role.get("roleTemplateId") or "").lower() for role in directory_roles}
        if templates & APP_ROLE_TEMPLATES:
            return True
        if allowed is False:
            return False
    return None


def can_grant_app_roles(directory_roles):
    """True, False or None (unknown): may the user grant the hosted agents their app roles in B4?"""
    if directory_roles is None:
        return None
    templates = {(role.get("roleTemplateId") or "").lower() for role in directory_roles}
    return bool(templates & GRANT_ROLE_TEMPLATES)


def image_problem(image):
    if not image:
        return f"image {VM_IMAGE} is not offered in this region"
    properties = image.get("properties") or {}
    generation = image.get("hyperVGeneration") or properties.get("hyperVGeneration")
    architecture = image.get("architecture") or properties.get("architecture")
    if generation != "V2" or (architecture or "x64").lower() != "x64":
        return f"image {VM_IMAGE} is not a Generation 2 x64 image here (found {generation}, {architecture})"
    return None


def size_problem(skus, location):
    entry = next((sku for sku in skus or [] if sku.get("name") == VM_SIZE), None)
    if not entry:
        return f"VM size {VM_SIZE} is not offered in {location}"
    for restriction in entry.get("restrictions") or []:
        if restriction.get("type") == "Location":
            return (f"VM size {VM_SIZE} is restricted for this subscription in {location} "
                    f"({restriction.get('reasonCode', 'no reason given')}); request access or choose another region")
    return None


def quota_shortfalls(models, usages, deployed):
    """(errors, warnings). deployed maps existing deployment names to capacity; a redeploy reuses that quota."""
    by_name = {(usage.get("name") or {}).get("value"): usage for usage in usages or []}
    errors, warnings = [], []
    for model in models:
        key = f"OpenAI.{model['sku']}.{model['name']}"
        usage = by_name.get(key)
        if usage is None:
            warnings.append(f"no quota entry {key}; the deployment will show whether the model is available")
            continue
        available = usage.get("limit", 0) - usage.get("currentValue", 0) + deployed.get(model["name"], 0)
        if available < model["capacity"]:
            errors.append(f"{key}: {model['capacity']} needed, {available:g} available. Lower capacity in "
                          "[[models]], request quota, or choose another region")
    return errors, warnings


def session_check(config, az):
    """Confirm the signed-in Azure CLI session still matches the configuration."""
    account = az("account", "show", "--query", "{id:id, tenant:tenantId, name:name, user:user}")
    if not account:
        raise PreflightError("Azure CLI is not signed in. Run az login --tenant <tenant ID>.")
    if account["id"].lower() != config.subscription_id:
        raise PreflightError(f"Azure CLI uses subscription {account['id']}, but config/mvp.toml names "
                             f"{config.subscription_id}. Run az account set --subscription {config.subscription_id}.")
    if (account.get("user") or {}).get("type") != "user":
        raise PreflightError("Sign in to Azure CLI as a user (az login), not a service principal or managed identity.")
    user = az("ad", "signed-in-user", "show", "--query", "{id:id, upn:userPrincipalName}")
    if (user.get("upn") or "").lower() != config.operator_upn.lower():
        raise PreflightError(f"Azure CLI is signed in as {user.get('upn')}, but operator_upn is "
                             f"{config.operator_upn}. Sign in as that user or correct config/mvp.toml.")
    return {"tenant_id": account["tenant"], "operator_object_id": user["id"], "subscription_name": account["name"]}


def run(config, az, out=print):
    errors, warnings = [], []
    with stage("Checking prerequisites"):
        version = (az("version") or {}).get("azure-cli", "0")
        if version_tuple(version) < MIN_AZ:
            raise PreflightError(f"Azure CLI {version} is too old; install {'.'.join(map(str, MIN_AZ))} or later.")
        try:
            bicep = az.text("bicep", "version")
        except AzError:
            az.text("bicep", "install")
        else:
            if version_tuple(bicep) < MIN_BICEP:
                az.text("bicep", "upgrade")
        cloud = az("cloud", "show", "--query", "name")
        if cloud != "AzureCloud":
            raise PreflightError(f"Azure CLI targets {cloud}; this MVP supports the public AzureCloud only.")
        identity = session_check(config, az)
        subscription = f"/subscriptions/{config.subscription_id}"

        assignments = az("role", "assignment", "list", "--assignee", identity["operator_object_id"], "--scope",
                         subscription, "--include-inherited", "--include-groups",
                         "--query", "[].{role:roleDefinitionName, scope:scope}")
        if not has_required_roles(assignments, config.subscription_id):
            errors.append("you need Owner, or Contributor plus User Access Administrator (or Role Based Access "
                          "Control Administrator), on the subscription. If the role is eligible through "
                          "Privileged Identity Management, activate it, then sign in again")

        allowed, directory_roles = _app_registration(az)
        if allowed is False:
            errors.append("you may not register applications in this tenant. Ask for the Application Developer "
                          "role; the passport app's sign-in needs an app registration")
        elif allowed is None:
            warnings.append("could not confirm that you may register applications; stage B needs this")
        if can_grant_app_roles(directory_roles) is False:
            warnings.append(f"no active {GRANT_ROLES} role: stage B grants the hosted agents their app roles "
                            "(B4) and needs one. If it is eligible through Privileged Identity Management, activate "
                            "it before stage B, then sign in again")

        if config.deploy_fabric:
            access = fabric_access(az)
            if access is False:
                errors.append("Microsoft Fabric refuses you: it is not activated for you in this tenant, and Azure "
                              f"will refuse to create the Fabric capacity. {FABRIC_ACTIVATION}")
            elif access is None:
                warnings.append("could not confirm that Microsoft Fabric is activated for you; if A1 fails on the "
                                "Fabric capacity, see the deployment guide's troubleshooting")

        states = provider_states(az)
        missing = missing_providers(required_providers(config), states)
        if missing:
            with stage(f"Registering {len(missing)} resource provider(s) together: {', '.join(missing)}"):
                register_providers(az, missing, states)

        problem = image_problem(az.find("vm", "image", "show", "--urn", VM_IMAGE, "--location", config.location))
        if problem:
            errors.append(problem)
        problem = size_problem(az("vm", "list-skus", "--location", config.location, "--size", VM_SIZE,
                                  "--resource-type", "virtualMachines"), config.location)
        if problem:
            errors.append(problem)

        group = az.find("group", "show", "--name", config.names.resource_group)
        if group:
            if (group.get("tags") or {}).get("project") != PROJECT_TAG:
                errors.append(f"resource group {config.names.resource_group} exists but was not created by this "
                              f"MVP (no project={PROJECT_TAG} tag). Choose another prefix")
            if (group.get("location") or "").lower() != config.location:
                errors.append(f"resource group {config.names.resource_group} is in {group.get('location')}, "
                              f"not {config.location}")

        quota_errors, quota_warnings = quota_shortfalls(
            config.models, az("cognitiveservices", "usage", "list", "--location", config.location),
            _deployed_capacity(az, config) if group else {})
        errors += quota_errors
        warnings += quota_warnings

        if config.vm_access == "jit":
            pricing = az.find("security", "pricing", "show", "--name", "VirtualMachines") or {}
            if pricing.get("pricingTier") != "Standard" or pricing.get("subPlan") != "P2":
                errors.append("vm_access = jit needs Microsoft Defender for Servers Plan 2 on the subscription. "
                              "Enable it with the subscription owner, or use vm_access = source-ip")
    for warning in warnings:
        out(f"warning: {warning}")
    if errors:
        raise PreflightError("Prerequisites not met:\n  - " + "\n  - ".join(errors))
    return {**identity, "warnings": warnings}


def _app_registration(az):
    try:
        policy = az.rest("get", f"{GRAPH}/policies/authorizationPolicy")
    except AzError:
        policy = None
    try:
        roles = (az.rest("get", f"{GRAPH}/me/transitiveMemberOf/microsoft.graph.directoryRole") or {}).get("value")
    except AzError:
        roles = None
    return can_create_apps(policy, roles), roles


def fabric_access(az):
    """True when Fabric answers the operator, False when it refuses them (not signed up), None when unknown."""
    try:
        az.rest("get", f"{FABRIC_API}/v1/workspaces", resource=FABRIC_API)
    except AzError as error:
        return False if FABRIC_REFUSED.search(error.stderr) else None
    return True


def _deployed_capacity(az, config):
    capacity = {}
    group = config.names.resource_group
    for account in az("cognitiveservices", "account", "list", "--resource-group", group, "--query", "[].name") or []:
        for deployment in az("cognitiveservices", "account", "deployment", "list", "--resource-group", group,
                             "--name", account, "--query", "[].{name:name, capacity:sku.capacity}") or []:
            capacity[deployment["name"]] = capacity.get(deployment["name"], 0) + (deployment.get("capacity") or 0)
    return capacity
