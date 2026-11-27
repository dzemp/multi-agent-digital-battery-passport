"""Remove everything the MVP created: Fabric workspace, Foundry account (purged), resource group, app registration.

Order matters. A locked retention policy makes the storage account undeletable, so it is checked before anything is
deleted. The Foundry account is deleted and purged first because its agent network link would otherwise keep the
agents subnet, and so the resource group, from being deleted. Its capability hosts and project must be gone before
the account is deleted.
"""

import time

from . import MvpError, fabric, preflight
from .az import AzError
from .config import MARKER, PROJECT_TAG

ARM = "https://management.azure.com"
FOUNDRY_API = "2026-09-01"
RESOURCE_DELETE_TIMEOUT = 15 * 60
SUBNET_TIMEOUT = 25 * 60
POLL = 30


def _wait(condition, timeout, sleep, clock):
    deadline = clock() + timeout
    while True:
        if condition():
            return True
        if clock() >= deadline:
            return False
        sleep(POLL)


def _gone(az, url):
    try:
        az.rest("get", url)
    except AzError as error:
        if error.not_found:
            return True
        raise
    return False


def _retention_policies(az, group, container):
    """[(account, state)] for the digest containers in the group."""
    policies = []
    for account in az("storage", "account", "list", "--resource-group", group, "--query", "[].name") or []:
        policy = az.find("storage", "container", "immutability-policy", "show", "--resource-group", group,
                         "--account-name", account, "--container-name", container)
        if policy and policy.get("state"):
            policies.append((account, policy["state"]))
    return policies


def _fabric_workspace(config, az, group_exists, out, remaining):
    name = config.names.fabric_workspace
    try:
        workspace = fabric.find_workspace(az, name)
    except (AzError, MvpError) as error:
        remaining.append(f"Fabric workspace {name}: could not be checked ({str(error).splitlines()[0]})")
        return
    if not workspace:
        return
    if not fabric.owned(workspace):
        remaining.append(f"Fabric workspace {name}: left in place because it was not created by this MVP")
        return
    try:
        fabric.delete_workspace(az, workspace["id"])
    except AzError:
        capacities = az("resource", "list", "--resource-group", config.names.resource_group, "--resource-type",
                        "Microsoft.Fabric/capacities", "--query", "[].id") if group_exists else []
        try:
            for capacity in capacities or []:
                fabric.set_capacity(az, capacity, active=True)
            fabric.delete_workspace(az, workspace["id"])
        except (AzError, MvpError) as error:
            remaining.append(f"Fabric workspace {name}: delete it in the Fabric portal ({str(error).splitlines()[0]})")
            return
    out(f"Deleted Fabric workspace {name}.")


def _delete_foundry_resource(az, resource_id, description, sleep, clock):
    url = f"{ARM}{resource_id}?api-version={FOUNDRY_API}"
    try:
        az.rest("delete", url)
    except AzError as error:
        if error.not_found:
            return
        raise
    if not _wait(lambda: _gone(az, url), RESOURCE_DELETE_TIMEOUT, sleep, clock):
        raise MvpError(f"The {description} was not removed within {RESOURCE_DELETE_TIMEOUT // 60} minutes. "
                       "Rerun teardown later.")


def _foundry(config, az, sleep, clock, out):
    group = config.names.resource_group
    for account in az("cognitiveservices", "account", "list", "--resource-group", group, "--query",
                      "[].{id:id, name:name}") or []:
        project = f"{account['id']}/projects/{config.names.foundry_project}"
        _delete_foundry_resource(az, f"{project}/capabilityHosts/agents",
                                 f"agents capability host on {account['name']}", sleep, clock)
        _delete_foundry_resource(az, project, f"project {config.names.foundry_project} on {account['name']}",
                                 sleep, clock)
        for other in (az.rest("get", f"{ARM}{account['id']}/capabilityHosts?api-version={FOUNDRY_API}")
                      or {}).get("value", []):
            _delete_foundry_resource(az, other["id"], f"account capability host {other['id']}", sleep, clock)
        az("cognitiveservices", "account", "delete", "--resource-group", group, "--name", account["name"])
        az("cognitiveservices", "account", "purge", "--location", config.location, "--resource-group", group,
           "--name", account["name"])
        out(f"Deleted and purged Foundry account {account['name']}.")

    def released():
        links = az.find("network", "vnet", "subnet", "show", "--resource-group", group, "--vnet-name",
                        config.names.vnet, "--name", config.names.agents_subnet, "--query", "serviceAssociationLinks")
        return not links

    if not _wait(released, SUBNET_TIMEOUT, sleep, clock):
        out("warning: the agents subnet still has a service link; the resource group delete may fail. If it does, "
            "wait 30 minutes and rerun teardown.")


def _app_registration(config, az, out, remaining):
    name = config.names.app_registration
    for app in az("ad", "app", "list", "--filter", f"displayName eq '{name}'", "--query",
                  "[].{appId:appId, name:displayName, tags:tags}") or []:
        if app.get("name") == name and MARKER in (app.get("tags") or []):
            az("ad", "app", "delete", "--id", app["appId"])
            out(f"Deleted app registration {name}.")
        else:
            remaining.append(f"app registration {name} ({app.get('appId')}): left in place, not created by this MVP")


def run(config, az, state, yes=False, keep_app_registration=False, confirm=input, out=print, sleep=time.sleep,
        clock=time.monotonic):
    preflight.session_check(config, az)
    names = config.names
    group = az.find("group", "show", "--name", names.resource_group)
    if group and (group.get("tags") or {}).get("project") != PROJECT_TAG:
        raise MvpError(f"Resource group {names.resource_group} has no project={PROJECT_TAG} tag; it was not "
                       "created by this MVP. Nothing was deleted.")
    if group:
        locked = [account for account, policy in _retention_policies(az, names.resource_group,
                                                                     names.digest_container) if policy == "Locked"]
        if locked:
            raise MvpError(f"Locked retention policy on {', '.join(locked)}: Azure blocks deleting it until the "
                           "retention period ends. Nothing was deleted.")

    out(f"This permanently deletes resource group {names.resource_group}"
        + (", its Fabric workspace" if config.deploy_fabric else "")
        + ("" if keep_app_registration else f" and app registration {names.app_registration}")
        + ". Synthetic data and the ledger digests are lost.")
    if not yes and confirm(f"Type {names.resource_group} to confirm: ").strip() != names.resource_group:
        raise MvpError("Cancelled; nothing was deleted.")

    remaining = []
    if config.deploy_fabric:
        _fabric_workspace(config, az, bool(group), out, remaining)
    if group:
        for account, _ in _retention_policies(az, names.resource_group, names.digest_container):
            try:
                az("storage", "container", "immutability-policy", "delete", "--resource-group",
                   names.resource_group, "--account-name", account, "--container-name", names.digest_container,
                   "--if-match", "*")
            except AzError as error:
                if not error.not_found:
                    remaining.append(f"retention policy on {account}: {str(error).splitlines()[0]}")
        _foundry(config, az, sleep, clock, out)
        out(f"Deleting resource group {names.resource_group}; this can take a while.")
        az("group", "delete", "--name", names.resource_group, "--yes")
    az.find("deployment", "sub", "delete", "--name", names.deployment)
    if not keep_app_registration:
        _app_registration(config, az, out, remaining)

    if az("group", "exists", "--name", names.resource_group):
        remaining.append(f"resource group {names.resource_group} still exists")
    else:
        state.clear()
    marker = f"/resourcegroups/{names.resource_group.lower()}/"
    for deleted in az("cognitiveservices", "account", "list-deleted", "--query", "[].id") or []:
        if marker in deleted.lower():
            remaining.append(f"soft-deleted Foundry account {deleted.rsplit('/', 1)[-1]}: purge it")
    if remaining:
        raise MvpError("Teardown incomplete:\n  - " + "\n  - ".join(remaining))
    out("Teardown complete." + (f" App registration {names.app_registration} was kept." if keep_app_registration
                                else " Nothing from this MVP remains."))
