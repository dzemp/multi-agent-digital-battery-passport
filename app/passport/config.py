"""Settings come from environment variables so the same image runs locally and in Azure.

An administrator can override some of them on the Settings page (settings_store.py); create_app merges the
saved overrides into the environment before calling Settings.from_env.
"""

import base64
import json
import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import parties

# Monitoring workbooks: (name, environment variable, sidebar label, full title)
WORKBOOKS = (
    ("executive", "DBP_WORKBOOK_EXECUTIVE", "Executive", "Executive service and cost"),
    ("compliance", "DBP_WORKBOOK_COMPLIANCE", "Compliance", "Compliance and review"),
    ("agents", "DBP_WORKBOOK_AGENTS", "Agents and AI", "Agents and AI quality"),
    ("operations", "DBP_WORKBOOK_OPERATIONS", "Platform operations", "Platform operations and security"),
)
PORTAL_HOSTS = ("portal.azure.com", "portal.azure.us", "portal.azure.cn")


def check_workbook_link(name, value):
    """A workbook link opens the Azure portal in a new window, so only portal addresses are accepted."""
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname not in PORTAL_HOSTS or parts.username or parts.password \
            or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError(f"{name} must be an https:// link to the Azure portal ({', '.join(PORTAL_HOSTS)}).")
    return value


def decode_b64_json(env, name):
    encoded = env.get(name, "")
    try:
        value = json.loads(base64.b64decode(encoded, validate=True)) if encoded else {}
    except ValueError as error:
        raise ValueError(f"{name} must be base64-encoded JSON.") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must decode to a JSON object.")
    return value


def check_prices(prices):
    """Same shape as the MVP's prices_file. Prices are never guessed; a missing model stays unpriced."""
    if not prices:
        return {}
    models = prices.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("DBP_PRICES_B64 needs a 'models' object.")
    for price in models.values():
        values = ([price.get("input_per_million"), price.get("output_per_million")] if isinstance(price, dict)
                  else [None])
        if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("Each DBP_PRICES_B64 model needs non-negative input_per_million and output_per_million.")
    if not isinstance(prices.get("currency"), str) or not prices["currency"]:
        raise ValueError("DBP_PRICES_B64 needs a currency.")
    return prices


@dataclass(frozen=True)
class Settings:
    auth_mode: str = "easyauth"  # easyauth (Container Apps) or local (PC only)
    store: str = "memory"  # memory or sql
    blobs: str = "local"  # local or azure
    blob_root: str = "output/blobs"
    blob_account_url: str = ""
    sql_server: str = ""
    sql_database: str = ""
    client_id: str = ""
    worker: bool = True
    worker_interval_seconds: float = 2.0
    max_upload_bytes: int = 5 * 1024 * 1024
    party_registry: dict = field(default_factory=dict)  # dbp-party-registry-1: public keys and permissions
    agent_mode: bool = False
    foundry_config: dict = field(default_factory=dict)
    dts_endpoint: str = ""
    dts_taskhub: str = ""
    public_base_url: str = ""  # printed in QR codes; empty means the address the page was opened with
    prices: dict = field(default_factory=dict)  # model token prices; empty means cost is "not measured"
    digest_account_url: str = ""  # Blob account holding the anchored SQL ledger digests
    workbooks: dict = field(default_factory=dict)  # monitoring workbook name -> Azure portal link

    @staticmethod
    def from_env(env=None):
        env = os.environ if env is None else env
        party_registry = decode_b64_json(env, "DBP_PARTY_REGISTRY_B64")
        parties.check_registry(party_registry)
        foundry_config = decode_b64_json(env, "DBP_FOUNDRY_CONFIG_B64")
        prices = check_prices(decode_b64_json(env, "DBP_PRICES_B64"))
        public_base_url = env.get("DBP_PUBLIC_BASE_URL", "").rstrip("/")
        if public_base_url and (urlsplit(public_base_url).scheme != "https" or not urlsplit(public_base_url).netloc):
            raise ValueError("DBP_PUBLIC_BASE_URL must be an https:// address.")
        workbooks = {name: check_workbook_link(key, env[key]) for name, key, _, _ in WORKBOOKS if env.get(key)}
        settings = Settings(
            auth_mode=env.get("DBP_AUTH_MODE", "easyauth"),
            store=env.get("DBP_STORE", "memory"),
            blobs=env.get("DBP_BLOBS", "local"),
            blob_root=env.get("DBP_BLOB_ROOT", "output/blobs"),
            blob_account_url=env.get("DBP_BLOB_ACCOUNT_URL", ""),
            sql_server=env.get("DBP_SQL_SERVER", ""),
            sql_database=env.get("DBP_SQL_DATABASE", ""),
            client_id=env.get("AZURE_CLIENT_ID", ""),
            worker=env.get("DBP_WORKER", "on") != "off",
            worker_interval_seconds=float(env.get("DBP_WORKER_INTERVAL", "2")),
            party_registry=party_registry,
            agent_mode=env.get("DBP_AGENT_MODE", "off") == "on",
            foundry_config=foundry_config,
            dts_endpoint=env.get("DBP_DTS_ENDPOINT", "").strip(),
            dts_taskhub=env.get("DBP_DTS_TASKHUB", "").strip(),
            public_base_url=public_base_url,
            prices=prices,
            digest_account_url=env.get("DBP_DIGEST_ACCOUNT_URL", ""),
            workbooks=workbooks,
        )
        if settings.auth_mode not in ("easyauth", "local"):
            raise ValueError("DBP_AUTH_MODE must be easyauth or local")
        if settings.auth_mode == "local" and env.get("CONTAINER_APP_NAME"):
            raise ValueError("DBP_AUTH_MODE=local trusts unsigned headers; it is refused inside Container Apps")
        if settings.store not in ("memory", "sql") or settings.blobs not in ("local", "azure"):
            raise ValueError("DBP_STORE must be memory or sql; DBP_BLOBS must be local or azure")
        if settings.store == "sql" and not (settings.sql_server and settings.sql_database):
            raise ValueError("DBP_STORE=sql needs DBP_SQL_SERVER and DBP_SQL_DATABASE")
        if settings.blobs == "azure" and not settings.blob_account_url:
            raise ValueError("DBP_BLOBS=azure needs DBP_BLOB_ACCOUNT_URL")
        if settings.agent_mode and (not settings.dts_endpoint or not settings.dts_taskhub):
            raise ValueError("DBP_AGENT_MODE=on needs DBP_DTS_ENDPOINT and DBP_DTS_TASKHUB.")
        return settings
