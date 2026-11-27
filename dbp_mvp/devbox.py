"""Developer VM commands: power state, RDP access and password reset."""

import secrets
import string
from datetime import datetime, timedelta, timezone

from . import MvpError
from .az import temporary_file
from .config import VM_ADMIN, public_ipv4
from .progress import stage

ARM = "https://management.azure.com"
JIT_API = "2020-01-01"
JIT_HOURS = 3
SYMBOLS = "-_.!@#"


def generate_password(length=20):
    """Random administrator password meeting Windows complexity rules; printed once, never stored."""
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        value = secrets.choice(string.ascii_letters) + "".join(secrets.choice(alphabet) for _ in range(length - 1))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value) and any(c in SYMBOLS for c in value)):
            return value


def vm_id(config):
    return (f"/subscriptions/{config.subscription_id}/resourceGroups/{config.names.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{config.names.vm}")


def power_state(az, config):
    codes = az("vm", "get-instance-view", "--resource-group", config.names.resource_group, "--name",
               config.names.vm, "--query", "instanceView.statuses[].code") or []
    return next((code.split("/", 1)[1] for code in codes if code.startswith("PowerState/")), "unknown")


def ensure_running(az, config):
    if power_state(az, config) != "running":
        with stage(f"Starting {config.names.vm}"):
            az("vm", "start", "--resource-group", config.names.resource_group, "--name", config.names.vm)


def start(az, config, out=print):
    ensure_running(az, config)
    out(f"{config.names.vm} is running. Auto-shutdown is set for {config.vm_auto_shutdown} ({config.time_zone}).")


def stop(az, config, out=print):
    with stage(f"Stopping and deallocating {config.names.vm}"):
        az("vm", "deallocate", "--resource-group", config.names.resource_group, "--name", config.names.vm)
    out("Deallocated: compute charges stop; the disk and public IP address are still billed.")


def jit_url(config, action=""):
    return (f"{ARM}/subscriptions/{config.subscription_id}/resourceGroups/{config.names.resource_group}"
            f"/providers/Microsoft.Security/locations/{config.location}/jitNetworkAccessPolicies/default"
            f"{action}?api-version={JIT_API}")


def access(az, config, ip=None, out=print, now=None):
    """Allow RDP from ip (default: client_ip): update the NSG rule, or request a JIT window."""
    ip = ip or config.client_ip
    if not public_ipv4(ip):
        raise MvpError(f"{ip} is not a public IPv4 address.")
    group = config.names.resource_group
    if config.vm_access == "source-ip":
        rule = ("--resource-group", group, "--nsg-name", config.names.nsg, "--name", config.names.rdp_rule)
        # Defender or a subscription policy can remove the template's rule; recreate it with the template's values.
        if az.find("network", "nsg", "rule", "show", *rule, "--query", "name") is None:
            az("network", "nsg", "rule", "create", *rule, "--priority", "100", "--access", "Allow",
               "--direction", "Inbound", "--protocol", "Tcp", "--source-address-prefixes", f"{ip}/32",
               "--source-port-ranges", "*", "--destination-address-prefixes", "*",
               "--destination-port-ranges", "3389")
            out(f"The RDP rule {config.names.rdp_rule} was missing and has been recreated.")
        else:
            az("network", "nsg", "rule", "update", *rule, "--source-address-prefixes", f"{ip}/32")
        out(f"RDP is allowed from {ip} only.")
    else:
        _jit(az, config, ip, now or datetime.now(timezone.utc))
        out(f"JIT access requested for {ip} for {JIT_HOURS} hours. Wait a minute for Defender to open the rule.")
    if ip != config.client_ip:
        out(f"Set client_ip = \"{ip}\" in config/mvp.toml: rerunning the bootstrap resets access to client_ip.")


def _jit(az, config, ip, now):
    policy = az.rest("get", jit_url(config))
    if not policy:
        raise MvpError("No JIT policy found. Rerun the bootstrap with vm_access = \"jit\".")
    target = vm_id(config).lower()
    machines = (policy.get("properties") or {}).get("virtualMachines") or []
    changed, found = False, False
    for machine in machines:
        if (machine.get("id") or "").lower() != target:
            continue
        for port in machine.get("ports") or []:
            if port.get("number") == 3389:
                found = True
                if port.get("allowedSourceAddressPrefix") != ip:
                    port["allowedSourceAddressPrefix"], changed = ip, True
    if not found:
        raise MvpError(f"The JIT policy does not cover RDP on {config.names.vm}. Rerun the bootstrap.")
    if changed:
        az.rest("put", jit_url(config), {"kind": policy.get("kind", "Basic"), "location": config.location,
                                         "properties": {"virtualMachines": machines}})
    end = (now + timedelta(hours=JIT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    az.rest("post", jit_url(config, "/initiate"), {
        "virtualMachines": [{"id": vm_id(config), "ports": [
            {"number": 3389, "allowedSourceAddressPrefix": ip, "endTimeUtc": end}]}],
        "justification": "Battery passport MVP developer access"})


def reset_password(az, config, out=print):
    """Set a new administrator password through the VM agent; ARM redeployments cannot change it."""
    ensure_running(az, config)
    password = generate_password()
    with temporary_file(password, suffix=".txt") as path, stage(f"Resetting the {VM_ADMIN} password"):
        az("vm", "user", "update", "--resource-group", config.names.resource_group, "--name", config.names.vm,
           "--username", VM_ADMIN, "--password", f"@{path}")
    out(f"New password for {VM_ADMIN}: {password}")
    out("It is shown once and stored nowhere. Save it in your password manager.")
    return password
