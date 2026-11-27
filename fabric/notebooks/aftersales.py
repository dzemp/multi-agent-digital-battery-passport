"""The three aftersales notebook activities. Spark replaces only this delivery's Delta partition."""

import json

from data.aftersales_flow import AFTER_STEPS, after_bronze, after_bytes, after_folder, after_gold, after_silver
from data.pipeline.flows import FolderStore
from .spark_store import SPARK_TYPES, SparkStore


def _after_columns(names):
    return tuple((n, "long" if n in ("line", "receipt") else "string") for n in names.split())


AFTER_TABLES = {
    "bronze": {"aftersales_inputs": _after_columns("delivery_id header_json"),
               "aftersales_receipts": _after_columns("delivery_id receipt source_delivery_id source_system "
                                                      "received_utc entity receipt_json receipt_sha256")},
    "silver": {"aftersales_rows": _after_columns("delivery_id receipt line source_delivery_id entity source_key "
                                                 "status reason record_json")}}


class AftersalesFolderStore(FolderStore):
    """The local equivalent uses the same stage code; each local root already identifies one snapshot."""

    def replace_rows(self, lakehouse, table, rows):
        path = self.root / lakehouse / "Tables" / f"{table}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(after_bytes(rows))


class AftersalesSparkStore(SparkStore):
    schemas = AFTER_TABLES

    def read_table(self, lakehouse, table):
        from delta.tables import DeltaTable

        path = self.table_path(lakehouse, table)
        if not DeltaTable.isDeltaTable(self.spark, path):
            return []
        frame = self.spark.read.format("delta").load(path)
        return [row.asDict() for row in frame.where(frame.delivery_id == self.delivery_id).collect()]

    def replace_rows(self, lakehouse, table, rows):
        columns = AFTER_TABLES[lakehouse][table]
        schema = ", ".join(f"{name} {SPARK_TYPES[kind]}" for name, kind in columns)
        frame = self.spark.createDataFrame([tuple(r[k] for k, _ in columns) for r in rows], schema)
        # The strict delivery ID grammar is checked before constructing this predicate.
        (frame.write.format("delta").partitionBy("delivery_id").mode("overwrite")
         .option("replaceWhere", f"delivery_id = '{self.delivery_id}'").save(self.table_path(lakehouse, table)))


def run_after_step(step, store, delivery):
    folder = after_folder(delivery)

    def read(layer, table):
        return [r for r in store.read_table(layer, table) if r["delivery_id"] == delivery]

    if step == "aftersales_bronze":
        raw = store.read_file("ingest", f"{folder}/source.json")
        if raw is None:
            raise ValueError("missing aftersales source")
        inputs, receipts = after_bronze(json.loads(raw.decode("utf-8")), delivery)
        store.replace_rows("bronze", "aftersales_inputs", inputs)
        store.replace_rows("bronze", "aftersales_receipts", receipts)
        return {"receipts": len(receipts)}
    inputs = read("bronze", "aftersales_inputs")
    if len(inputs) != 1:
        raise ValueError("Bronze snapshot is missing or ambiguous")
    header = json.loads(inputs[0]["header_json"])
    receipts = read("bronze", "aftersales_receipts")
    if step == "aftersales_silver":
        rows = after_silver(header, receipts, delivery)
        store.replace_rows("silver", "aftersales_rows", rows)
        return {"rows": len(rows)}
    if step != "aftersales_gold":
        raise ValueError(f"Unknown aftersales step {step!r}")
    files = after_gold(header, receipts, read("silver", "aftersales_rows"), delivery)
    for name, data in files.items():  # envelope first, completion manifest last
        store.write_file("gold", f"{folder}/{name}", data)
    return json.loads(files["manifest.json"])["row_counts"]


def after_main(step, config, parameters, spark, store_type=AftersalesSparkStore):
    store = store_type(spark, config["workspace_id"], config["lakehouses"], AFTER_STEPS[step])
    store.delivery_id = parameters["delivery_id"]
    result = run_after_step(step, store, store.delivery_id)
    print(json.dumps(result, sort_keys=True))
    return result
