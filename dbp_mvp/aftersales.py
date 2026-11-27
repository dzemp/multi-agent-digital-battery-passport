"""Generate synthetic service inputs, run aftersales_flow, and stage verified Fabric bytes for app intake.

run_local is an explicit development/test equivalent. Its files are never consulted by run_cloud or gold_path.
"""

import json
import uuid

from data import simulator
from data.aftersales import make_source
from data.aftersales_flow import AFTER_STEPS, after_bytes, after_delivery, after_expected, after_folder
from fabric.notebooks.aftersales import AftersalesFolderStore, run_after_step

from . import MvpError, fabric, supply
from .config import OUTPUT


def _snapshot(state, source):
    seed = make_source(state)
    if source is None:
        source = seed
    for key in ("build_id", "series_id", "production_catalog"):
        if source[key] != seed[key]:
            raise ValueError(f"aftersales source does not match the prepared series: {key}")
    return source, after_delivery(source)


def _root(state, output):
    return simulator.build_paths(output, state)["package"].parent / "aftersales"


def gold_path(state, output=OUTPUT, *, source=None):
    """The cloud-only copy of Gold's exact aftersales.json for this build/series/delivery."""
    _, delivery = _snapshot(state, source)
    return _root(state, output) / "fabric" / delivery / "aftersales.json"


def run_local(state, *, output=OUTPUT, source=None):
    """Run the same three activities on local folders, returning the decoded Gold envelope."""
    source, delivery = _snapshot(state, source)
    store = AftersalesFolderStore(_root(state, output) / "local" / delivery)
    folder = after_folder(delivery)
    store.write_file("ingest", f"{folder}/source.json", after_bytes(source))
    for step in AFTER_STEPS:
        run_after_step(step, store, delivery)
    return json.loads(store.read_file("gold", f"{folder}/aftersales.json"))


def _save_verified(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def run_cloud(config, az, outputs, state, *, send=fabric.send, sleep=None, output=OUTPUT, out=print, source=None):
    """Run or verify an immutable Fabric delivery. Return the fetched envelope; never write a local substitute."""
    capacity = supply.capacity(config, outputs)
    try:
        source, delivery = _snapshot(state, source)
        folder = f"Files/{after_folder(delivery)}"
        raw = after_bytes(source)
        client = fabric.FabricClient(az, send=send, sleep=sleep)
        with fabric.active_capacity(az, capacity, sleep=sleep, out=out):
            items = supply.deploy_items(client, config.names.fabric_workspace, capacity, out,
                                        flows=("aftersales_flow",))
            ws, houses = items["workspace_id"], items["lakehouses"]
            lake = fabric.OneLake(client, ws)
            landed = lake.get(houses["ingest"], f"{folder}/source.json")
            if landed is None:
                lake.put(houses["ingest"], f"{folder}/source.json", raw)
            elif landed != raw:
                raise MvpError(f"Conflicting immutable aftersales source {delivery} in OneLake.")
            gold = houses["gold"]
            if lake.get(gold, f"{folder}/manifest.json") is None:
                fabric.run_job(client, ws, items["pipelines"]["aftersales_flow"], {"delivery_id": delivery}, out)
            expected = after_expected(source)
            fetched = {name: lake.get(gold, f"{folder}/{name}") for name in expected}
            for name, data in fetched.items():
                if data is None or data != expected[name]:
                    raise MvpError(f"Missing or mismatched Fabric Gold {delivery}/{name}; no aftersales import.")
        target = gold_path(state, output, source=source)
        _save_verified(target.with_name("manifest.json"), fetched["manifest.json"])
        _save_verified(target, fetched["aftersales.json"])
        out(f"  Verified Fabric aftersales delivery {delivery} for {source['series_id']}.")
        return json.loads(fetched["aftersales.json"])
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise MvpError(f"Aftersales delivery failed; no local substitute: {exc}") from exc
