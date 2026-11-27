"""The store the Fabric notebooks pass to the flows: Delta tables through OneLake paths, files through the notebook's
default lakehouse, which Fabric mounts at /lakehouse/default.

The notebooks embed this text after data/pipeline/flows.py, so FolderStore, TABLES and shape are already defined
there; the import below only serves the local tests and is removed when the notebook is generated. pyspark and delta
are imported inside the methods, so this file also loads where Spark is not installed.
"""

from pathlib import Path

from data.pipeline.flows import TABLES, FolderStore, shape

ONELAKE_HOST = "onelake.dfs.fabric.microsoft.com"
SPARK_TYPES = {"string": "STRING", "long": "BIGINT", "double": "DOUBLE", "boolean": "BOOLEAN"}
DEFAULT_FILES = "/lakehouse/default/Files"


class SparkStore(FolderStore):
    """Tables of every layer by OneLake path; files only in the notebook's default lakehouse."""

    schemas = TABLES

    def __init__(self, spark, workspace_id, lakehouses, files_lakehouse, files_root=DEFAULT_FILES):
        super().__init__(files_root)
        self.spark, self.workspace_id, self.lakehouses = spark, workspace_id, dict(lakehouses)
        self.files_lakehouse, self.files_root = files_lakehouse, Path(files_root)

    def table_path(self, lakehouse, table):
        if table not in self.schemas.get(lakehouse, {}) or lakehouse not in self.lakehouses:
            raise ValueError(f"Unknown table {lakehouse}.{table}")
        return f"abfss://{self.workspace_id}@{ONELAKE_HOST}/{self.lakehouses[lakehouse]}/Tables/{table}"

    def _file(self, lakehouse, path):
        if lakehouse != self.files_lakehouse:
            raise ValueError(f"This notebook reads and writes files in its default lakehouse "
                             f"{self.files_lakehouse} only, not in {lakehouse}.")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Invalid store path: {lakehouse}/{path}")
        return self.files_root.joinpath(*parts)

    def read_table(self, lakehouse, table):
        from delta.tables import DeltaTable

        path = self.table_path(lakehouse, table)
        if not DeltaTable.isDeltaTable(self.spark, path):
            return []
        return [row.asDict() for row in self.spark.read.format("delta").load(path).collect()]

    def replace_rows(self, lakehouse, table, rows):
        columns = TABLES[lakehouse][table]
        values = [tuple(row[name] for name, _ in columns) for row in shape(lakehouse, table, rows)]
        schema = ", ".join(f"{name} {SPARK_TYPES[kind]}" for name, kind in columns)
        frame = self.spark.createDataFrame(values, schema)
        writer = frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
        if table.startswith("aftersales_"):  # reset keeps partitioning for the next aftersales_flow write
            writer = writer.partitionBy("delivery_id")
        writer.save(self.table_path(lakehouse, table))
