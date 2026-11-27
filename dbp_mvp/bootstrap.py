"""Stage A, run on the operator's laptop: check, deploy the Azure resources, prepare the developer VM, connect.

A0 preflight -> A1 infrastructure (infra/main.bicep) -> A2 developer VM tools and checkout -> A3 access.
Finished phases are skipped while their inputs are unchanged; --from repeats a phase and every later one.
"""

import base64
import dataclasses
import hashlib
import re
import uuid
from pathlib import Path

from . import MvpError, devbox, fabric, preflight
from .az import AzError
from .config import INFRA, VM_ADMIN, VM_CHECKOUT
from .progress import stage
from .state import files_fingerprint, fingerprint

ORDER = ("A0", "A1", "A2", "A3")
SCRIPT = Path(__file__).with_name("devbox-setup.ps1")
MARKER_LINE = re.compile(r"DBP_MVP_DEVBOX: tools=(ready|failed) clone=(cloned|existing|failed|skipped) "
                         r"venv=(ready|skipped|failed)")
SETUP_LOG = r"C:\ProgramData\battery-passport\devbox-setup.log"
PUBLIC_IP_FEATURE = "AllowBringYourOwnPublicIpAddress"
FABRIC_SIGN_UP = re.compile(r"recogni[sz]ed by Microsoft Fabric|Sign up for Microsoft Fabric", re.IGNORECASE)
ROLE_EXISTS = re.compile(r"RoleAssignmentExists.*?existing role assignment is ([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})",
                         re.DOTALL)
RERUN = "Existing resources are kept; a bootstrap rerun reapplies the infrastructure template."


def deployment_advice(config, stderr):
    """Targeted recovery steps for known A1 prerequisite failures and pending DNS operations."""
    advice = []
    if "SubscriptionNotRegisteredForFeature" in stderr and PUBLIC_IP_FEATURE in stderr:
        feature = f"--namespace Microsoft.Network --name {PUBLIC_IP_FEATURE}"
        advice.append(
            f"Azure required the subscription feature Microsoft.Network/{PUBLIC_IP_FEATURE} to create a public IP "
            "address. The template uses no bring-your-own addresses: Azure allocates the developer VM's RDP address"
            + (" and the NAT gateway's address" if config.enable_nat_gateway else "")
            + ", but some subscriptions apply this approval-gated feature to those as well.\n"
            f"  1. Check it:    az feature show {feature} --query properties.state\n"
            f"  2. With the subscription owner's agreement, request it:  az feature register {feature}\n"
            "     Microsoft approves it; it stays Pending until then. If it stays Pending, or the error persists "
            "once it is Registered, open an Azure support request with this error and its correlation ID.\n"
            "  3. Once Registered:  az provider register --namespace Microsoft.Network, then rerun the bootstrap.")
    if FABRIC_SIGN_UP.search(stderr):
        advice.append("Microsoft Fabric does not know your tenant yet, so Azure cannot create the Fabric capacity. "
                      f"{preflight.FABRIC_ACTIVATION}. Then rerun the bootstrap.")
    existing = ROLE_EXISTS.search(stderr)
    if existing:
        assignment = str(uuid.UUID(existing.group(1)))
        advice.append(
            "A role the template grants was already granted outside the template (for example in the Azure "
            "portal), under a different assignment ID, so Azure refused the duplicate. Check the existing "
            f"assignment:  az role assignment list --all --query \"[?name=='{assignment}']\" -o table\n"
            "  If it is the same role for the same identity and scope, delete it so the template can own it:  "
            f"az role assignment delete --ids <id shown by the command above>\n"
            "  Then rerun the bootstrap; the template grants the same role again.")
    if "virtualnetworklinks" in stderr.lower() and "another operation is pending" in stderr.lower():
        advice.append(
            "Azure still has an operation queued for a private DNS virtual network link. Wait for the active "
            "deployment and link operation to finish before retrying; a failed parent deployment does not prove "
            "the service-side operation has finished. Do not delete the link or start concurrent bootstraps. "
            "The network template serializes its link writes, but cannot clear an operation from an earlier run. "
            "Check the link and resource-group deployments in the Azure portal. If the conflict persists with no "
            "deployment running, contact Azure Support with the original operation ID. After it clears, run "
            "'python -m dbp_mvp bootstrap --from A0' to refresh provider checks and resume. See docs/deployment.md "
            "section 7.")
    return advice


def _infrastructure_fingerprint(config, operator_object_id):
    parameters = config.bicep_parameters(operator_object_id, "", True)
    parameters.pop("vmAdminPassword")
    parameters.pop("createDevboxVm")
    return fingerprint({"parameters": parameters, "infra": files_fingerprint(INFRA)})


def _vm_exists(az, config):
    return az.find("vm", "show", "--resource-group", config.names.resource_group, "--name", config.names.vm,
                   "--query", "id") is not None


def _resume_capacities(config, az):
    """Resume existing paused Fabric capacities so the template can update them; returns the resumed IDs."""
    if not config.deploy_fabric:
        return []
    capacities = az.find("resource", "list", "--resource-group", config.names.resource_group, "--resource-type",
                         "Microsoft.Fabric/capacities", "--query", "[].id") or []
    resumed = []
    for capacity in capacities:
        if fabric.capacity_state(az, capacity) in fabric.PAUSED:
            with stage("Resuming the paused Fabric capacity so the template can update it"):
                fabric.set_capacity(az, capacity, active=True)
            resumed.append(capacity)
    return resumed


def _pause_capacities(az, capacities, out):
    for capacity in capacities:
        try:
            fabric.set_capacity(az, capacity, active=False)
        except (AzError, MvpError) as error:
            out(f"WARNING: the Fabric capacity could not be paused and is still billed: {error}\n"
                "Pause it in the Azure portal, or check it with python -m dbp_mvp status.")


def _summary(config, create_vm):
    names = config.names
    lines = [
        f"Deploy the battery passport MVP to subscription {config.subscription_id}, region {config.location}:",
        f"  resource group {names.resource_group}: network {names.vnet}, private endpoints, storage, container "
        "registry and Container Apps environment, monitoring, Foundry account and project",
        "  Azure SQL Database: paid General Purpose serverless, Standard-series (Gen5), 0.5 to 2 vCores; "
        "auto-pause after 60 idle minutes",
        f"  developer VM {names.vm} ({'created now' if create_vm else 'existing, left unchanged'}), "
        f"RDP {'from ' + config.client_ip + ' only' if config.vm_access == 'source-ip' else 'through Defender JIT'}",
    ]
    if config.deploy_fabric:
        lines.append(f"  Microsoft Fabric capacity {config.fabric_sku} (paused after deployment)")
    if config.enable_nat_gateway:
        lines.append("  NAT gateway for the agents subnet")
    lines += [
        "",
        "Costs: these resources are billed to the subscription. The VM shuts down daily at "
        f"{config.vm_auto_shutdown} ({config.time_zone}); stop it with 'python -m dbp_mvp vm stop'. Fabric bills "
        "only while its capacity is Active. Remove everything with 'python -m dbp_mvp teardown'.",
        "Licensing: the VM runs Windows 11 Enterprise with licence type Windows_Client. Continue only if you have "
        "eligible Windows 10/11 Enterprise per-user licences with multitenant hosting rights; this tool cannot "
        "check them.",
        "Data: synthetic only. This is a reference MVP, not a production or compliance system.",
    ]
    return "\n".join(lines)


def flatten_outputs(outputs):
    """ARM deployment outputs ({name: {type, value}}) as {name: value}."""
    return {key: value.get("value") for key, value in (outputs or {}).items()}


def _what_if(config, az, identity, out):
    create_vm = not _vm_exists(az, config)
    parameters = config.bicep_parameters(identity["operator_object_id"], devbox.generate_password(), create_vm)
    out("What-if only: nothing is created or recorded.")
    with az.parameter_file(parameters) as parameter_file:
        az.stream("deployment", "sub", "what-if", "--name", config.names.deployment, "--location",
                  config.location, "--template-file", str(INFRA / "main.bicep"), "--parameters", parameter_file)


def _infrastructure(config, az, state, identity, yes, confirm, out):
    """A1. Returns (outputs, password or None). The password exists only when the VM was created now."""
    phase_fingerprint = _infrastructure_fingerprint(config, identity["operator_object_id"])
    vm_exists = _vm_exists(az, config)
    if state.done("A1", phase_fingerprint) and vm_exists:
        out("A1 infrastructure: unchanged, skipped.")
        return state.outputs("A1"), None

    previous = az.find("deployment", "sub", "show", "--name", config.names.deployment, "--query",
                       "{state:properties.provisioningState, location:location}")
    if previous and previous.get("state") in ("Running", "Accepted"):
        raise MvpError(f"Deployment {config.names.deployment} is still running. Wait for it to finish "
                       "(Azure portal > Subscription > Deployments), then rerun the bootstrap.")
    if previous and (previous.get("location") or "").lower() != config.location:
        raise MvpError(f"Deployment {config.names.deployment} was made in {previous.get('location')}. Delete it "
                       f"with az deployment sub delete --name {config.names.deployment}, or use another prefix.")

    create_vm = not vm_exists
    out(_summary(config, create_vm))
    if not yes and confirm("\nType yes to deploy: ").strip().lower() != "yes":
        raise MvpError("Cancelled; nothing was deployed.")

    password = devbox.generate_password()
    parameters = config.bicep_parameters(identity["operator_object_id"], password, create_vm)
    # Azure refuses to update a paused Fabric capacity ("Service is not ready to be updated").
    resumed = _resume_capacities(config, az)
    try:
        with az.parameter_file(parameters) as parameter_file, stage("A1 Deploying infrastructure (long-running)"):
            result = az("deployment", "sub", "create", "--name", config.names.deployment, "--location",
                        config.location, "--template-file", str(INFRA / "main.bicep"), "--parameters",
                        parameter_file, "--query", "properties.outputs")
    except AzError as error:
        _pause_capacities(az, resumed, out)
        advice = deployment_advice(config, error.stderr)
        if not advice:
            raise
        raise MvpError("\n\n".join([str(error), *advice, RERUN])) from error
    except BaseException:
        _pause_capacities(az, resumed, out)
        raise
    outputs = flatten_outputs(result)
    state.record("A1", phase_fingerprint, outputs)

    if create_vm:
        out(f"\nVM administrator: {VM_ADMIN}\nPassword: {password}\nShown now and in the final summary only; "
            "it is stored nowhere. Save it in your password manager. Replace it later with "
            "'python -m dbp_mvp vm reset-password'.")
    if config.deploy_fabric and outputs.get("fabricCapacityId"):
        with stage("Pausing the Fabric capacity until stage B needs it"):
            fabric.set_capacity(az, outputs["fabricCapacityId"], active=False)
    return outputs, password if create_vm else None


def parse_marker(text):
    matches = MARKER_LINE.findall(text or "")
    if not matches:
        return None
    tools, clone, venv = matches[-1]
    return {"tools": tools, "clone": clone, "venv": venv}


def _developer_vm(config, az, state, out):
    """A2. Install the tools, clone this folder and copy the configuration onto the VM through Run Command."""
    devbox.ensure_running(az, config)
    repository = config.repository()
    settings = config.to_toml(repository)
    machine = az("vm", "show", "--resource-group", config.names.resource_group, "--name", config.names.vm,
                 "--query", "{id:id, created:timeCreated}")
    phase_fingerprint = fingerprint({"repository": dataclasses.asdict(repository), "settings": settings,
                                     "script": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(), "vm": machine})
    if state.done("A2", phase_fingerprint):
        out("A2 developer VM: unchanged, skipped.")
        return state.outputs("A2"), repository

    parameters = [f"{key}={value}" for key, value in (("RepoUrl", repository.url), ("RepoRef", repository.ref),
                                                       ("Subfolder", repository.subfolder)) if value]
    parameters.append("ConfigBase64=" + base64.b64encode(settings.encode("utf-8")).decode("ascii"))
    with stage("A2 Installing developer tools on the VM (long-running)"):
        result = az("vm", "run-command", "invoke", "--resource-group", config.names.resource_group, "--name",
                    config.names.vm, "--command-id", "RunPowerShellScript", "--scripts", f"@{SCRIPT}",
                    "--parameters", *parameters)
    messages = "\n".join(item.get("message") or "" for item in (result or {}).get("value", []))
    status = parse_marker(messages)
    if not status:
        raise MvpError(f"The VM setup script did not report a result. Sign in to the VM and read {SETUP_LOG}, "
                       "then rerun with --from A2.")
    if status["tools"] == "failed":
        raise MvpError(f"Tool installation failed on the VM. Read {SETUP_LOG} on the VM, then rerun with --from A2.")
    state.record("A2", phase_fingerprint, status)
    return status, repository


def _checkout_folder(repository):
    return VM_CHECKOUT + ("\\" + repository.subfolder.replace("/", "\\") if repository.subfolder else "")


def _access(config, az, state, outputs, setup, repository, identity, password, out):
    """A3. Always runs: make sure the VM is on and RDP is open from client_ip, then print the next steps."""
    devbox.ensure_running(az, config)
    if config.vm_access == "source-ip":
        rule = az.find("network", "nsg", "rule", "show", "--resource-group", config.names.resource_group,
                       "--nsg-name", config.names.nsg, "--name", config.names.rdp_rule, "--query",
                       "{one:sourceAddressPrefix, many:sourceAddressPrefixes}") or {}
        if rule.get("one") != f"{config.client_ip}/32" or rule.get("many"):
            devbox.access(az, config, config.client_ip, out=out)
    else:
        devbox.access(az, config, config.client_ip, out=out)
    state.record("A3", fingerprint(config.identity), {})

    folder = _checkout_folder(repository)
    lines = ["", "Stage A complete. Connect to the developer VM with Remote Desktop:",
             f"  address  {outputs.get('vmPublicIp', '(see the Azure portal)')}",
             f"  user     {VM_ADMIN}"]
    if password:
        lines.append(f"  password {password}   (shown once; not stored)")
    else:
        lines.append("  password the one shown when the VM was created ('python -m dbp_mvp vm reset-password' "
                     "sets a new one)")
    if setup.get("clone") in ("failed", "skipped"):
        lines += ["", "The VM could not clone the repository (private repository or no URL). On the VM:",
                  f"  git clone <repository URL> {VM_CHECKOUT}",
                  rf"  copy C:\ProgramData\battery-passport\mvp.toml {folder}\config\mvp.toml",
                  f"  cd {folder}; python -m venv .venv; .\\.venv\\Scripts\\python -m pip install -r requirements.txt"]
    elif setup.get("venv") != "ready":
        lines += ["", "The Python environment was not created. On the VM:",
                  f"  cd {folder}; python -m venv .venv; .\\.venv\\Scripts\\python -m pip install -r requirements.txt"]
    lines += ["", "Then, in PowerShell on the VM:",
              f"  az login --tenant {identity['tenant_id']}",
              f"  cd {folder}",
              r"  .\.venv\Scripts\python -m dbp_mvp deploy",
              "", f"The VM shuts down at {config.vm_auto_shutdown} ({config.time_zone}). Stop it earlier from this "
              "laptop with 'python -m dbp_mvp vm stop'; start it with 'python -m dbp_mvp vm start'."]
    out("\n".join(lines))


def run(config, az, state, start=None, what_if=False, yes=False, confirm=input, out=print):
    if what_if:
        _what_if(config, az, preflight.run(config, az, out), out)
        return
    state.bind(config.identity)
    if start:
        state.forget_from(start, ORDER)

    phase_fingerprint = fingerprint(config.as_dict())
    if state.done("A0", phase_fingerprint):
        preflight.session_check(config, az)
        out("A0 preflight: unchanged, skipped (sign-in confirmed).")
    else:
        result = preflight.run(config, az, out)
        state.record("A0", phase_fingerprint, {key: result[key] for key in
                                                ("tenant_id", "operator_object_id", "subscription_name")})
    identity = state.outputs("A0")

    outputs, password = _infrastructure(config, az, state, identity, yes, confirm, out)
    setup, repository = _developer_vm(config, az, state, out)
    _access(config, az, state, outputs, setup, repository, identity, password, out)
