"""Saved settings: point the app at another Azure environment without rebuilding or redeploying it.

Order of precedence: a value saved on the Settings page, then the environment variable the deployment set
(the MVP's Bicep), then the built-in default. Only the keys in FIELDS can be saved; sign-in mode, identity,
agents, Foundry and prices stay fixed at deployment. No password is stored: SQL and Blob use the app's managed
identity. The Application Insights connection string is masked on screen and never echoed back.

Where the saved values live (location()):
  DBP_SETTINGS_BLOB_URL set        one JSON blob, read and written with the app's managed identity (Azure)
  else CONTAINER_APP_NAME set      nowhere: the container file system is lost on restart, so saving is off
  else                             DBP_SETTINGS_FILE, default output/settings.json (a PC)
Saved values apply when the app starts. A missing, unreadable or invalid file never stops the app: it starts
on the deployment values and the Settings page says why.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .config import PORTAL_HOSTS, WORKBOOKS, Settings, check_workbook_link

log = logging.getLogger("passport")
SCHEMA = 1
MAX_VALUE = 2048
APP_INSIGHTS = "APPLICATIONINSIGHTS_CONNECTION_STRING"
DEFAULT_FILE = "output/settings.json"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# The app sends its managed identity's tokens to the SQL server and storage accounts, so those fields accept
# only Azure SQL Database and Blob Storage hosts. Any other host would receive a valid token.
SQL_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.database\.windows\.net)?$", re.IGNORECASE)
BLOB_HOST = re.compile(r"^[a-z0-9]{3,24}\.blob\.core\.windows\.net$")
MONITOR_HOSTS = (".applicationinsights.azure.com", ".monitor.azure.com", ".services.visualstudio.com")
HOST_TEXT = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
DATABASE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str  # choice, host, name, account, public, secret or link
    help: str = ""
    default: str = ""
    choices: tuple = ()  # (value, label) pairs for kind "choice"
    placeholder: str = ""


GROUPS = (
    ("data", "Passport database", "database", "Where passports, holds and the audit trail are stored."),
    ("files", "Documents and ledger proof", "evidence",
     "Where evidence files and passport snapshots are kept, and where the anchored ledger digests are read."),
    ("monitoring", "Monitoring", "monitor",
     "Business events for Application Insights, and the monitoring workbooks shown under Monitoring in the sidebar. "
     "For a workbook link, open the workbook in the Azure portal and copy the address from the browser."),
    ("public", "Public passport", "passport", "The address printed in the QR code on each passport."),
)
FIELDS = (
    Field("DBP_STORE", "Database type", "data", "choice", default="memory",
          choices=(("sql", "Azure SQL Database"), ("memory", "In memory (a PC only; lost on restart)"))),
    Field("DBP_SQL_SERVER", "SQL server", "data", "host", placeholder="sql-dbp-example.database.windows.net",
          help="An Azure SQL Database server: its name or host name only. The app signs in with its managed identity; "
               "no password is stored."),
    Field("DBP_SQL_DATABASE", "Database name", "data", "name", placeholder="sqldb-passport"),
    Field("DBP_BLOBS", "Document storage", "files", "choice", default="local",
          choices=(("azure", "Azure Blob Storage"), ("local", "Local folder (a PC only)"))),
    Field("DBP_BLOB_ACCOUNT_URL", "Storage account address", "files", "account",
          placeholder="https://stdbpexample.blob.core.windows.net",
          help="The Blob endpoint (https://<account>.blob.core.windows.net) only, without a SAS token. The app signs "
               "in with its managed identity."),
    Field("DBP_DIGEST_ACCOUNT_URL", "Ledger digest storage address", "files", "account",
          placeholder="https://stdigestexample.blob.core.windows.net",
          help="Holds the anchored SQL ledger digests. Leave empty and the decision trail says the check is not "
               "available."),
    Field(APP_INSIGHTS, "Application Insights connection string", "monitoring", "secret",
          placeholder="InstrumentationKey=...;IngestionEndpoint=https://...",
          help="Application Insights > Overview > Connection string. Leave empty and no business events are sent."),
    *(Field(key, f"{title} workbook", "monitoring", "link", placeholder="https://portal.azure.com/#@...")
      for _, key, _, title in WORKBOOKS),
    Field("DBP_PUBLIC_BASE_URL", "Public address", "public", "public", placeholder="https://passport.example.com",
          help="Leave empty to use the address the page was opened with."),
)
FIELD = {f.key: f for f in FIELDS}
EDITABLE = tuple(FIELD)


class SavingOff(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(key, value):
    value = (value or "").strip()
    return value.rstrip("/") if FIELD[key].kind in ("account", "public") else value


def connection_parts(value):
    parts = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        name, sep, text = item.partition("=")
        if not sep or not name.strip():
            raise ValueError("not a Name=value list")
        parts[name.strip().lower()] = text.strip()
    return parts


def connection_string_problem(value):
    try:
        parts = connection_parts(value)
    except ValueError:
        return "Paste the whole connection string: Name=value pairs separated by semicolons."
    if not GUID.fullmatch(parts.get("instrumentationkey", "")):
        return "The connection string needs an InstrumentationKey (a GUID)."
    allowed = ", ".join(f"*{suffix}" for suffix in MONITOR_HOSTS)
    for name, endpoint in parts.items():
        if not name.endswith("endpoint"):
            continue
        address = urlsplit(endpoint)
        if (address.scheme != "https" or not monitor_host(address.hostname)
                or address.netloc.lower() != address.hostname):
            return f"Every endpoint in the connection string must be an https:// Azure Monitor address ({allowed})."
    suffix = parts.get("endpointsuffix")
    if suffix is not None and not monitor_host(f"dc.{suffix}"):
        return f"EndpointSuffix must be an Azure Monitor domain ({allowed})."
    return None


def monitor_host(host):
    return bool(host) and HOST_TEXT.fullmatch(host) is not None and host.lower().endswith(MONITOR_HOSTS)


def https_netloc_only(parts):
    """True when the address is scheme://host with no port, user name or password."""
    return parts.scheme == "https" and bool(parts.hostname) and parts.netloc.lower() == parts.hostname


def masked(value):
    """Enough to recognise which resource is configured, never the whole string."""
    if not value:
        return "Not configured"
    try:
        parts = connection_parts(value)
    except ValueError:
        return "Configured (not readable)"
    host = urlsplit(parts.get("ingestionendpoint", "")).hostname
    suffix = f" · sends to {host}" if host else ""
    return f"Configured · key ending {parts.get('instrumentationkey', '')[-4:]}{suffix}"


def problem(key, value):
    """Why one value cannot be saved, in words an administrator can act on; None when it is fine."""
    f = FIELD[key]
    if len(value) > MAX_VALUE:
        return f"Too long: at most {MAX_VALUE:,} characters."
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "Remove line breaks and control characters."
    if f.kind == "choice":
        return None if value in dict(f.choices) else "Choose one of the options."
    if not value:
        return None
    if f.kind == "host":
        if "://" in value or ":" in value or "/" in value:
            return "Enter the host name only, without https:// or a port."
        return None if SQL_HOST.fullmatch(value) else \
            "Enter an Azure SQL Database server, such as sql-dbp-example.database.windows.net."
    if f.kind == "name":
        return None if DATABASE.fullmatch(value) else "Use 1 to 128 letters, digits, hyphens or underscores."
    if f.kind in ("account", "public"):
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            return "Enter an https:// address."
        if parts.username or parts.password:
            return "Remove the user name or password from the address."
        if "?" in value or "#" in value:
            return "Remove the part from ? onwards: no SAS token or query is needed."
        if f.kind == "account" and parts.path not in ("", "/"):
            return "Enter the account address only, without a container or file."
        if f.kind == "account" and not (https_netloc_only(parts) and BLOB_HOST.fullmatch(parts.hostname)):
            return "Enter an Azure Blob Storage address: https://<account>.blob.core.windows.net, without a port."
        return None
    if f.kind == "link":
        try:
            check_workbook_link(key, value)
        except ValueError:
            return f"Paste a workbook link from the Azure portal ({', '.join(PORTAL_HOSTS)})."
        return None
    return connection_string_problem(value)


def values_of(env, saved=None):
    """The editable values: saved overrides on top of the environment on top of the defaults."""
    saved = saved or {}
    return {k: normalize(k, saved[k] if k in saved else env.get(k, FIELD[k].default)) for k in EDITABLE}


def running_values(settings, app_insights=""):
    """The editable values the running app actually uses."""
    values = {"DBP_STORE": settings.store, "DBP_SQL_SERVER": settings.sql_server,
              "DBP_SQL_DATABASE": settings.sql_database, "DBP_BLOBS": settings.blobs,
              "DBP_BLOB_ACCOUNT_URL": settings.blob_account_url, "DBP_DIGEST_ACCOUNT_URL": settings.digest_account_url,
              APP_INSIGHTS: app_insights, "DBP_PUBLIC_BASE_URL": settings.public_base_url}
    values.update({key: settings.workbooks.get(name, "") for name, key, _, _ in WORKBOOKS})
    return {k: normalize(k, v) for k, v in values.items()}


def parse(text):
    """Read a saved file strictly: an unknown key or a bad value rejects the whole file."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("values"), dict):
        raise ValueError(f"it is not a schema {SCHEMA} settings file")
    values = {}
    for key, value in data["values"].items():
        if key not in FIELD:
            raise ValueError(f"{str(key)[:60]} cannot be set on the Settings page")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = normalize(key, value)
        error = problem(key, value)
        if error:
            raise ValueError(f"{key}: {error}")
        values[key] = value
    meta = {"updated_utc": str(data.get("updated_utc") or "")[:40],
            "updated_by": str(data.get("updated_by") or "")[:200]}
    return values, meta


def document(values, actor, now=None):
    return json.dumps({"schema": SCHEMA, "updated_utc": now or utc_now(), "updated_by": actor,
                       "values": dict(sorted(values.items()))}, indent=2) + "\n"


class LocalFile:
    def __init__(self, path):
        self.path = Path(path)

    def describe(self):
        return f"Local file {self.path}"

    def read(self):
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.path)  # a reader sees the old file or the new one, never half of one


class BlobFile:
    """One JSON blob in the app's storage account. Blob versioning keeps every earlier save."""

    def __init__(self, url, credential=None):
        self.url, self.credential = url, credential

    def describe(self):
        parts = urlsplit(self.url)
        return f"Blob {parts.hostname}{parts.path}"

    def _client(self):
        from azure.storage.blob import BlobClient

        if self.credential is None:
            from azure.identity import DefaultAzureCredential

            self.credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return BlobClient.from_blob_url(self.url, credential=self.credential)

    def read(self):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            with self._client() as client:
                return client.download_blob().readall().decode("utf-8")
        except ResourceNotFoundError:
            return None

    def write(self, text):
        from azure.storage.blob import ContentSettings

        with self._client() as client:
            client.upload_blob(text.encode("utf-8"), overwrite=True,
                               content_settings=ContentSettings(content_type="application/json"))


def location(env):
    """(file, None) when saving is possible, else (None, the reason it is off)."""
    url = (env.get("DBP_SETTINGS_BLOB_URL") or "").strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.username \
                or len([p for p in parts.path.split("/") if p]) < 2:
            return None, ("Saving is off: DBP_SETTINGS_BLOB_URL must be an https:// address of a blob "
                          "(account/container/file) without a SAS token.")
        return BlobFile(url), None
    if env.get("CONTAINER_APP_NAME"):
        return None, ("Saving is off: this container has no settings blob (DBP_SETTINGS_BLOB_URL) and its file system "
                      "is lost on restart. Redeploy the app (deploy --from B3), which adds one, or change the "
                      "deployment values.")
    return LocalFile(env.get("DBP_SETTINGS_FILE") or DEFAULT_FILE), None


def _credential(client_id):
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    return ManagedIdentityCredential(client_id=client_id) if client_id else \
        DefaultAzureCredential(exclude_interactive_browser_credential=True)


def probe(name, action):
    """Run one check. The page shows only the error type and Azure error code, so a reply's content is never echoed;
    the full error goes to the app log."""
    try:
        action()
        return {"name": name, "ok": True, "text": "Connected"}
    except Exception as error:  # noqa: BLE001 - show the failure; never let a test break the page
        log.warning("Settings connection test %s failed: %s", name, error)
        code = getattr(error, "error_code", None)
        code = f" ({code})" if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else ""
        return {"name": name, "ok": False, "text": f"{type(error).__name__}{code}. See the app log for details."}


def test_connections(values, client_id=""):
    """Reach each Azure service the values point at, with the app's own identity. Sends no telemetry."""
    checks = []
    if values["DBP_STORE"] == "sql":
        def sql():
            from .sql_store import SqlStore

            SqlStore(values["DBP_SQL_SERVER"], values["DBP_SQL_DATABASE"], client_id).ping()
        checks.append(("Azure SQL Database", sql))
    if values["DBP_BLOBS"] == "azure":
        def blob():
            from .blobs import AzureBlobs

            AzureBlobs(values["DBP_BLOB_ACCOUNT_URL"], _credential(client_id)).ping()
        checks.append(("Blob Storage (passports container)", blob))
    if values["DBP_DIGEST_ACCOUNT_URL"]:
        def digests():
            from azure.storage.blob import ContainerClient

            from .ledger import DIGEST_CONTAINER

            with ContainerClient(values["DBP_DIGEST_ACCOUNT_URL"], DIGEST_CONTAINER,
                                 credential=_credential(client_id)) as container:
                next(iter(container.list_blobs(results_per_page=1)), None)
        checks.append(("Ledger digests (sqldbledgerdigests container)", digests))
    return [probe(name, action) for name, action in checks]


def display(f, value):
    if f.kind == "secret":
        return masked(value)
    if f.kind == "choice":
        return dict(f.choices).get(value, value or "Not set")
    return value or "Not set"


class SettingsAdmin:
    """What the Settings page needs: where overrides live, what the deployment set and what is running."""

    def __init__(self, file=None, base_env=None, saved=None, meta=None, tester=None, load_error=None, save_off=None):
        self.file = file
        self.base_env = dict(base_env) if base_env is not None else None
        self.saved, self.meta = dict(saved or {}), dict(meta or {})
        self.tester = tester or test_connections
        self.load_error, self.save_off = load_error, save_off
        self.running = None

    @classmethod
    def load(cls, env, tester=None):
        file, save_off = location(env)
        admin = cls(file, env, tester=tester, save_off=save_off)
        if file is not None:
            try:
                text = file.read()
                admin.saved, admin.meta = parse(text) if text else ({}, {})
            except Exception as error:  # noqa: BLE001 - a broken or unreachable file must never stop the app
                detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else ""
                admin.load_error = (f"The saved settings could not be read ({type(error).__name__}"
                                    f"{': ' + detail if detail else ''}). The app started with the deployment values; "
                                    "saving again replaces the file.")
        return admin

    @classmethod
    def fixed(cls, reason):
        return cls(save_off=reason)

    def env(self):
        """The environment the app starts with: deployment values with the saved overrides on top."""
        return {**(self.base_env or {}), **self.saved}

    def rejected(self, reason):
        """The saved values did not combine into valid settings, so the app runs on the deployment values."""
        self.saved, self.meta = {}, {}
        self.load_error = (f"The saved settings were not applied: {reason} The app started with the deployment "
                           "values shown below; saving the form replaces the file.")

    def start(self, settings, env=None):
        self.running = running_values(settings, (env or {}).get(APP_INSIGHTS, ""))
        if self.base_env is None:
            self.base_env = dict(self.running)
        return self

    def deployment(self):
        return values_of(self.base_env or {})

    def current(self):
        return values_of(self.base_env or {}, self.saved)

    def pending(self):
        """Keys whose saved value differs from what the app runs with: these need a restart."""
        if self.running is None:
            return []
        current = self.current()
        return [k for k in EDITABLE if current[k] != self.running[k]]

    def where(self):
        return self.file.describe() if self.file is not None else "Nowhere: saving is off"

    def restart_hint(self):
        env = self.base_env or {}
        app = env.get("CONTAINER_APP_NAME")
        if not app:
            return {"azure": False, "text": "Stop the app (Ctrl+C) and start it again."}
        return {"azure": True, "command": f"az containerapp revision restart -n {app} "
                f"-g {env.get('DBP_RESOURCE_GROUP') or '<resource-group>'} "
                f"--revision {env.get('CONTAINER_APP_REVISION') or '<revision>'}"}

    def submitted(self, data):
        """Form fields to values. A blank secret keeps the current value unless 'clear' is ticked."""
        current = self.current()
        values = {}
        for f in FIELDS:
            typed = normalize(f.key, data.get(f.key, ""))
            if f.kind == "secret":
                typed = typed or ("" if data.get(f"clear_{f.key}") else current[f.key])
            values[f.key] = typed
        return values

    def validate(self, values):
        """(field errors, form error). Checks each value, then the combination exactly as start-up will."""
        errors = {k: e for k in EDITABLE if (e := problem(k, values[k]))}
        in_azure = bool((self.base_env or {}).get("CONTAINER_APP_NAME"))
        if values["DBP_STORE"] == "sql":
            for key in ("DBP_SQL_SERVER", "DBP_SQL_DATABASE"):
                if not values[key]:
                    errors.setdefault(key, "Required for Azure SQL Database.")
        elif in_azure:
            errors.setdefault("DBP_STORE", "Not available in Azure: data in memory is lost when the app restarts.")
        if values["DBP_BLOBS"] == "azure":
            if not values["DBP_BLOB_ACCOUNT_URL"]:
                errors.setdefault("DBP_BLOB_ACCOUNT_URL", "Required for Azure Blob Storage.")
        elif in_azure:
            errors.setdefault("DBP_BLOBS", "Not available in Azure: the container file system is lost on restart.")
        if errors:
            return errors, None
        try:
            Settings.from_env({**(self.base_env or {}), **values})
        except ValueError as error:
            return {}, str(error)
        return {}, None

    def save(self, values, actor):
        """Store only what differs from the deployment, so a redeployment's new values still show through."""
        if self.file is None:
            raise SavingOff(self.save_off or "Saving is off.")
        base, before = self.deployment(), self.current()
        overrides = {k: values[k] for k in EDITABLE if k in values and values[k] != base[k]}
        now = utc_now()
        self.file.write(document(overrides, actor, now))
        self.saved, self.meta, self.load_error = overrides, {"updated_utc": now, "updated_by": actor}, None
        changed = [k for k in EDITABLE if self.current()[k] != before[k]]
        log.info("settings saved; changed keys: %s", ", ".join(changed) or "none")  # names only, never values
        return changed

    def reset(self, actor):
        return self.save(self.deployment(), actor)

    def view(self, settings, values=None, errors=None):
        current, base = self.current(), self.deployment()
        running = self.running or current
        shown = values or current
        groups = []
        for name, title, icon, text in GROUPS:
            fields = [{"key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "placeholder": f.placeholder,
                       "choices": f.choices, "value": "" if f.kind == "secret" else shown[f.key],
                       "display": display(f, current[f.key]), "deployment": display(f, base[f.key]),
                       "has_value": bool(current[f.key]), "overridden": f.key in self.saved,
                       "pending": current[f.key] != running[f.key], "error": (errors or {}).get(f.key)}
                      for f in FIELDS if f.group == name]
            groups.append({"name": name, "title": title, "icon": icon, "text": text, "fields": fields,
                           "overrides": sum(1 for x in fields if x["overridden"])})
        pending = self.pending()
        return {"groups": groups, "load_error": self.load_error, "save_off": self.save_off,
                "pending": [FIELD[k].label for k in pending], "restart": self.restart_hint() if pending else None,
                "saved_count": len(self.saved), "where": self.where(), "meta": self.meta,
                "fixed": fixed_facts(settings, self.where())}


def fixed_facts(settings, where):
    identity = (f"User-assigned managed identity (client ID ending {settings.client_id[-4:]})" if settings.client_id
                else "Default Azure credential (az login on a PC)")
    prices = settings.prices
    return [
        ("Sign-in", "Microsoft Entra ID (Container Apps authentication)" if settings.auth_mode == "easyauth"
         else "Local identity headers (a PC only)"),
        ("App identity", identity),
        ("AI agents", "On" if settings.agent_mode else "Off"),
        ("Foundry project", "Configured" if settings.foundry_config else "Not configured"),
        ("Model prices", f"Configured ({prices['currency']})" if prices else "Not set: cost shows \"not measured\""),
        ("Registered parties", f"{len(settings.party_registry.get('parties') or [])} (DBP_PARTY_REGISTRY_B64, "
                               "public keys only)"),
        ("Saved settings", where),
    ]
