"""The single, non-secret parameter set: config/mvp.toml, copied from config/mvp.example.toml.

The VM password and the app secret are generated at runtime and never written to config or state.
"""

import dataclasses
import ipaddress
import json
import math
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import MvpError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "mvp.toml"
INFRA = ROOT / "infra"
OUTPUT = ROOT / "output"

MARKER = "battery-passport-mvp"  # tag/description on objects outside the resource group that teardown may delete
PROJECT_TAG = "battery-passport"  # resource group tag set by infra/main.bicep
VM_SIZE = "Standard_D4s_v5"
VM_ADMIN = "mvpadmin"
VM_IMAGE = "MicrosoftWindowsDesktop:windows-11:win11-24h2-ent:latest"
VM_CHECKOUT = r"C:\battery-passport"
FABRIC_SKUS = ("F2", "F4", "F8", "F16", "F32", "F64")
# Must cover every model in app/passport/agent_contracts.py ROLES (checked by tests/test_infra.py).
# DataZoneStandard processes prompts within the location's Microsoft-defined data zone (the EU zone for swedencentral).
DEFAULT_MODELS = (
    {"name": "gpt-6.1-sol", "version": "2026-09-29", "sku": "DataZoneStandard", "capacity": 200},
    {"name": "gpt-5.6-luna", "version": "2026-07-09", "sku": "DataZoneStandard", "capacity": 200},
    {"name": "gpt-5.6-terra", "version": "2026-07-09", "sku": "DataZoneStandard", "capacity": 200},
)

REQUIRED = ("subscription_id", "operator_upn", "prefix", "client_ip")
DEFAULTS = {
    "location": "swedencentral",
    "vm_access": "source-ip",
    "vm_auto_shutdown": "19:00",
    "time_zone": "W. Europe Standard Time",
    "deploy_fabric": True,
    "fabric_sku": "F2",
    "enable_nat_gateway": False,
    "warm_replica": False,
    "enable_alerts": False,
    "alert_email": "",
    "powerbi": False,
    "extra_batteries": 0,
    "repo_url": "",
    "repo_ref": "",
    "prices_file": "",
}
SCENARIOS = {"evaluation": False, "credential_negatives": False}
MODEL_KEYS = {"name", "version", "sku", "capacity"}
PRICE_KEYS = {"currency", "source", "models"}
TOKEN_PRICES = ("input_per_million", "output_per_million")

UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
EMAIL = re.compile(r"[^@\s'\"]+@[^@\s'\"]+\.[^@\s'\"]+")
PREFIX = re.compile(r"[a-z][a-z0-9]{2,5}")
LOCATION = re.compile(r"[a-z][a-z0-9]{1,39}")
TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9 .+()-]{1,63}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
SUBFOLDER = re.compile(r"[A-Za-z0-9._/-]{0,200}")
MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,63}")
MODEL_VERSION = re.compile(r"[A-Za-z0-9.-]{1,32}")
SKU = re.compile(r"[A-Za-z]{2,32}")
UNSAFE_URL = set('&|<>^%"\'\\` \t\r\n')


class ConfigError(MvpError):
    pass


def public_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_global


def _https_url(value):
    if not value:
        return True
    if UNSAFE_URL & set(value):
        return False
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc and not parts.query


def _ref(value):
    return not value or (bool(REF.fullmatch(value)) and ".." not in value and not value.endswith((".lock", "/")))


CHECKS = {
    "subscription_id": (UUID.fullmatch, "a subscription ID (GUID)"),
    "operator_upn": (EMAIL.fullmatch, "a sign-in name such as operator@contoso.com"),
    "prefix": (PREFIX.fullmatch, "3-6 lowercase letters or digits, starting with a letter"),
    "client_ip": (public_ipv4, "your public IPv4 address, without /32"),
    "location": (LOCATION.fullmatch, "an Azure region name such as swedencentral"),
    "vm_access": (lambda v: v in ("source-ip", "jit"), "source-ip or jit"),
    "vm_auto_shutdown": (TIME.fullmatch, "a 24-hour time HH:mm"),
    "time_zone": (TIME_ZONE.fullmatch, "a Windows time zone ID such as W. Europe Standard Time"),
    "fabric_sku": (lambda v: v in FABRIC_SKUS, "one of " + ", ".join(FABRIC_SKUS)),
    "extra_batteries": (lambda v: 0 <= v <= 50, "between 0 and 50"),
    "alert_email": (lambda v: not v or EMAIL.fullmatch(v), "an e-mail address"),
    "repo_url": (_https_url, "empty or an https URL without credentials or query"),
    "repo_ref": (_ref, "empty or a branch or tag name"),
}


@dataclass(frozen=True)
class Names:
    resource_group: str
    deployment: str
    vm: str
    nsg: str
    rdp_rule: str
    vnet: str
    agents_subnet: str
    app: str
    app_registration: str
    fabric_workspace: str
    foundry_project: str
    digest_container: str

    @classmethod
    def of(cls, prefix):
        return cls(resource_group=f"{prefix}-rg", deployment=f"{prefix}-mvp", vm=f"{prefix}-devbox",
                   nsg=f"{prefix}-devbox-nsg", rdp_rule="AllowRdpFromClient", vnet=f"{prefix}-vnet",
                   agents_subnet="agents", app=f"{prefix}-passport", app_registration=f"{prefix}-passport",
                   fabric_workspace=f"{prefix}-passport", foundry_project="passport",
                   digest_container="sqldbledgerdigests")


@dataclass(frozen=True)
class Repository:
    url: str
    ref: str
    subfolder: str


@dataclass(frozen=True)
class Config:
    subscription_id: str
    operator_upn: str
    prefix: str
    client_ip: str
    location: str
    vm_access: str
    vm_auto_shutdown: str
    time_zone: str
    deploy_fabric: bool
    fabric_sku: str
    enable_nat_gateway: bool
    warm_replica: bool
    enable_alerts: bool
    alert_email: str
    powerbi: bool
    extra_batteries: int
    repo_url: str
    repo_ref: str
    prices_file: str
    scenarios: dict
    models: tuple
    prices: dict
    path: Path = dataclasses.field(default=CONFIG, compare=False)

    @property
    def names(self):
        return Names.of(self.prefix)

    @property
    def identity(self):
        return {"subscription_id": self.subscription_id, "prefix": self.prefix, "location": self.location}

    def as_dict(self):
        values = dataclasses.asdict(self)
        values.pop("path")
        values["models"] = [dict(model) for model in self.models]
        return values

    def bicep_parameters(self, operator_object_id, password, create_vm):
        """Values for infra/main.bicep. Written to a temporary parameter file, never to a command line."""
        return {
            "prefix": self.prefix,
            "location": self.location,
            "operatorUpn": self.operator_upn,
            "operatorObjectId": operator_object_id,
            "clientIp": self.client_ip,
            "vmAccess": self.vm_access,
            "vmAdminUsername": VM_ADMIN,
            "vmAdminPassword": password,
            "vmSize": VM_SIZE,
            "createDevboxVm": create_vm,
            "vmAutoShutdownTime": self.vm_auto_shutdown,
            "timeZone": self.time_zone,
            "deployFabric": self.deploy_fabric,
            "fabricSku": self.fabric_sku,
            "enableNatGateway": self.enable_nat_gateway,
            "enableAlerts": self.enable_alerts,
            "alertEmail": self.alert_email,
            "models": [{"name": m["name"], "model": {"format": "OpenAI", "name": m["name"], "version": m["version"]},
                        "sku": {"name": m["sku"], "capacity": m["capacity"]}} for m in self.models],
        }

    def repository(self):
        """Where the developer VM clones this folder from. Defaults to this checkout's origin and upstream branch."""
        url, ref = self.repo_url, self.repo_ref
        if not url:
            url = _https_origin(_git("remote", "get-url", "origin") or "")
            if url and not ref:
                upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") or ""
                ref = upstream.split("/", 1)[1] if upstream.startswith("origin/") else ""
        subfolder = (_git("rev-parse", "--show-prefix") or "").strip("/")
        if not _https_url(url) or not _ref(ref):
            url, ref = "", ""
        if not SUBFOLDER.fullmatch(subfolder) or ".." in subfolder.split("/"):
            subfolder = ""
        return Repository(url=url, ref=ref, subfolder=subfolder)

    def to_toml(self, repository=None):
        """The same settings as TOML, for the copy written to the developer VM.

        The price list is copied inline, because the laptop's prices_file does not exist on the VM.
        """
        values = self.as_dict()
        if repository:
            values["repo_url"], values["repo_ref"] = repository.url, repository.ref
        values["prices_file"] = ""
        lines = ["# Written by the bootstrap from the laptop's config/mvp.toml. Not secret.",
                 "# Change settings on the laptop and rerun the bootstrap; this copy is overwritten.", ""]
        lines += [f"{key} = {_toml(values[key])}" for key in (*REQUIRED, *DEFAULTS)]
        if values["prices"]:
            lines.append(f"prices = {_toml(values['prices'])}")
        lines += ["", "[scenarios]"] + [f"{key} = {_toml(value)}" for key, value in values["scenarios"].items()]
        for model in values["models"]:
            lines += ["", "[[models]]"] + [f"{key} = {_toml(model[key])}" for key in ("name", "version", "sku",
                                                                                       "capacity")]
        return "\n".join(lines) + "\n"


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)} = {_toml(item)}"
                               for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def _git(*args):
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _https_origin(url):
    """https form of a git remote, without credentials. The VM clones anonymously or through Git Credential Manager."""
    url = url.strip()
    match = re.fullmatch(r"(?:ssh://)?[\w.-]+@([\w.-]+)[:/](.+)", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit(("https", host, parts.path, "", ""))


def _kind(default):
    return {bool: "true or false", int: "whole number", str: "quoted text"}[type(default)]


def parse(raw, path=CONFIG):
    problems = []
    unknown = sorted(set(raw) - set(REQUIRED) - set(DEFAULTS) - {"scenarios", "models", "prices"})
    if unknown:
        problems.append("unknown setting(s): " + ", ".join(unknown))
        if "use_free_sql_offer" in unknown:
            problems.append("Remove use_free_sql_offer from your settings to use the paid General Purpose "
                            "serverless database (Standard-series Gen5, 0.5 to 2 vCores).")
    values = {}
    for key in REQUIRED:
        value = raw.get(key, "")
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key} is required")
        else:
            values[key] = value.strip()
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if type(value) is not type(default):
            problems.append(f"{key} must be {_kind(default)}")
        else:
            values[key] = value.strip() if isinstance(value, str) else value
    for key, (check, meaning) in CHECKS.items():
        if key in values and not check(values[key]):
            problems.append(f"{key} must be {meaning}")
    if values.get("enable_alerts") and not values.get("alert_email"):
        problems.append("alert_email is required when enable_alerts = true")
    if "subscription_id" in values:
        values["subscription_id"] = values["subscription_id"].lower()

    scenarios = raw.get("scenarios", {})
    if not isinstance(scenarios, dict):
        problems.append("[scenarios] must be a table")
        scenarios = {}
    for key in sorted(set(scenarios) - set(SCENARIOS)):
        problems.append(f"unknown scenario: {key}")
    values["scenarios"] = {}
    for key, default in SCENARIOS.items():
        value = scenarios.get(key, default)
        if not isinstance(value, bool):
            problems.append(f"scenarios.{key} must be true or false")
        values["scenarios"][key] = value

    values["models"] = _models(raw.get("models"), problems)
    values["prices"] = _prices(raw.get("prices"), values.get("prices_file", ""), path, problems)
    if problems:
        raise ConfigError(f"{path}:\n  - " + "\n  - ".join(problems))
    return Config(**values, path=path)


def _prices(inline, prices_file, path, problems):
    """Model token prices from prices_file (a JSON file, relative to the config file) or, on the VM, inline."""
    if inline is not None and prices_file:
        problems.append("set either prices_file or prices, not both")
        return {}
    label = "prices"
    if prices_file:
        file = Path(prices_file)
        file = file if file.is_absolute() else Path(path).parent / file
        label = f"prices_file {file}"
        try:
            inline = json.loads(file.read_text(encoding="utf-8-sig"))
        except OSError as error:
            problems.append(f"{label} cannot be read ({error.strerror or error})")
            return {}
        except ValueError as error:
            problems.append(f"{label} is not valid JSON ({error})")
            return {}
    if inline is None:
        return {}
    if not isinstance(inline, dict):
        problems.append(f"{label} must be an object with currency, models and optionally source")
        return {}
    count = len(problems)
    if set(inline) - PRICE_KEYS:
        problems.append(f"{label}: unknown key(s) " + ", ".join(sorted(set(inline) - PRICE_KEYS)))
    currency, source, models = inline.get("currency"), inline.get("source", ""), inline.get("models")
    if not isinstance(currency, str) or not currency.strip() or len(currency) > 16:
        problems.append(f"{label}: currency must be a short text such as USD")
    if not isinstance(source, str) or len(source) > 200:
        problems.append(f"{label}: source must be text of at most 200 characters")
    if not isinstance(models, dict) or not models:
        problems.append(f"{label}: models must map one or more model names to their prices")
        models = {}
    for name, price in models.items():
        if not MODEL_NAME.fullmatch(name):
            problems.append(f"{label}: {name!r} is not a model name such as gpt-5.4")
        values = [price.get(key) for key in TOKEN_PRICES] if isinstance(price, dict) else [None]
        if not isinstance(price, dict) or set(price) != set(TOKEN_PRICES) or not all(
                type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in values):
            problems.append(f"{label}: {name} needs exactly input_per_million and output_per_million, each a "
                            "number of at least 0")
    if len(problems) > count:
        return {}
    prices = {"currency": currency.strip(), **({"source": source.strip()} if source.strip() else {})}
    prices["models"] = {name: {key: models[name][key] for key in TOKEN_PRICES} for name in sorted(models)}
    return prices


def _models(raw, problems):
    if raw is None:
        return tuple(dict(model) for model in DEFAULT_MODELS)
    if not isinstance(raw, list) or not raw or not all(isinstance(model, dict) for model in raw):
        problems.append("[[models]] must be one or more tables")
        return ()
    models = []
    for index, model in enumerate(raw, start=1):
        label = f"models #{index}"
        if set(model) - MODEL_KEYS:
            problems.append(f"{label}: unknown key(s) " + ", ".join(sorted(set(model) - MODEL_KEYS)))
        entry = {"name": model.get("name"), "version": model.get("version"),
                 "sku": model.get("sku", "DataZoneStandard"), "capacity": model.get("capacity")}
        if not isinstance(entry["name"], str) or not MODEL_NAME.fullmatch(entry["name"]):
            problems.append(f"{label}: name must be a model name such as gpt-5.4")
        if not isinstance(entry["version"], str) or not MODEL_VERSION.fullmatch(entry["version"]):
            problems.append(f"{label}: version must be a model version such as 2026-03-05")
        if not isinstance(entry["sku"], str) or not SKU.fullmatch(entry["sku"]):
            problems.append(f"{label}: sku must be a deployment type such as DataZoneStandard")
        capacity = entry["capacity"]
        if type(capacity) is not int or not 1 <= capacity <= 1000:
            problems.append(f"{label}: capacity must be a whole number from 1 to 1000")
        models.append(entry)
    names = [model["name"] for model in models]
    if len(names) != len(set(names)):
        problems.append("[[models]] names must be unique")
    return tuple(models)


def load(path=CONFIG, *, prices_file=None):
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config/mvp.example.toml to config/mvp.toml and fill in "
                          "the four required settings.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    if prices_file is not None:
        raw.pop("prices", None)
        raw["prices_file"] = str(Path(prices_file).resolve())
    return parse(raw, path)
