"""Regenerates fixtures/ (synthetic 'Contoso Shop' workspace, ground truth, dates). No real data.

Usage: python tools/make_fixtures.py [project_root]   (default: the folder above tools/)
"""
import json
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYN = os.path.join(ROOT, "fixtures", "synthetic_workspace")

NB_IDS = {}
PL_IDS = {}


def gid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def platform(kind, name, lid):
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": kind, "displayName": name},
        "config": {"version": "2.0", "logicalId": lid}}, indent=2)


META = '# META {\n# META   "language": "python",\n# META   "language_group": "synapse_pyspark"\n# META }\n'


def py_nb(cells):
    out = ["# Fabric notebook source\n"]
    for kind, body in cells:
        if kind == "md":
            out.append("# MARKDOWN ********************\n\n" + "\n".join("# " + ln for ln in body.splitlines()) + "\n")
            continue
        if kind == "sql":
            body = "\n".join("# MAGIC " + ln for ln in ("%%sql\n" + body).splitlines())
        out.append("# METADATA ********************\n\n" + META)
        out.append("# CELL ********************\n\n" + body.strip("\n") + "\n")
    out.append("# METADATA ********************\n\n" + META)
    return "\n".join(out)


def add_nb(i, name, text, ext="py"):
    NB_IDS[name] = gid(i)
    d = os.path.join(SYN, "notebooks", f"{name}.Notebook")
    write(os.path.join(d, ".platform"), platform("Notebook", name, gid(i)))
    write(os.path.join(d, f"notebook-content.{ext}"), text)


add_nb(1, "nb_ingest_orders", py_nb([("code", '''spark.sql("""
INSERT INTO bronze_shop.orders
SELECT * FROM staging.orders_raw
""")''')]))

add_nb(2, "nb_silver_orders", py_nb([
    ("md", "Silver orders"),
    ("sql", '''CREATE OR REPLACE TEMP VIEW v_orders AS
SELECT o.order_id, o.customer_id, o.order_date, o.amount, c.region
FROM bronze_shop.orders o
JOIN bronze_shop.customers c ON o.customer_id = c.customer_id'''),
    ("code", '''df = spark.sql("SELECT * FROM v_orders")
df.write.mode("overwrite").saveAsTable("silver_shop.orders")'''),
]))

add_nb(3, "nb_silver_customers", py_nb([("code", '''spark.sql("""
MERGE INTO silver_shop.customers AS t
USING (
  WITH latest AS (SELECT * FROM bronze_shop.customers)
  SELECT * FROM latest
) s ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""")''')]))

add_nb(4, "nb_gold_revenue", py_nb([("code", '''SCHEMA = "gold_shop"
TARGET = f"{SCHEMA}.revenue_daily"
query = f"""
INSERT OVERWRITE TABLE {TARGET}
SELECT o.order_date, SUM(o.amount) AS amount
FROM silver_shop.orders o
JOIN silver_shop.customers c ON o.customer_id = c.customer_id
GROUP BY o.order_date
"""
spark.sql(query)''')]))

add_nb(5, "nb_dynamic_export", py_nb([("code", '''tables = ["orders", "customers"]
for t in tables:
    df = spark.table(f"silver_shop.{t}")
    df.write.saveAsTable(output_name)''')]))

add_nb(6, "nb_helper_loader", py_nb([("code", '''src = read_lakehouse_table_path("silver_shop", "orders")
dst = write_lakehouse_table_path("gold_shop", "orders_snapshot")''')]))

add_nb(7, "nb_orchestrator", py_nb([
    ("code", '''import notebookutils
notebookutils.notebook.run("nb_silver_orders", 300)
notebookutils.notebook.run("nb_missing_nb", 300)'''),
    ("code", "%run nb_gold_revenue"),
]))
# the %run cell must be written as a MAGIC line
p = os.path.join(SYN, "notebooks", "nb_orchestrator.Notebook", "notebook-content.py")
t = open(p, encoding="utf-8").read().replace("\n%run nb_gold_revenue", "\n# MAGIC %run nb_gold_revenue")
write(p, t)

add_nb(8, "nb_generic_load", py_nb([("code", '''cfg_path = lakehouse_table_path("cfg", "load_cfg")
for row in rows:
    target = f"{row['dest_schema']}.{row['dest_table']}"
    spark.sql(f"INSERT INTO {target} SELECT * FROM staging.incoming")''')]))

add_nb(9, "nb_report_view", """-- Fabric notebook source

-- METADATA ********************

-- META {
-- META   "language": "sparksql"
-- META }

-- CELL ********************

CREATE OR REPLACE VIEW gold_shop.v_revenue AS
SELECT * FROM gold_shop.revenue_daily;

-- METADATA ********************

-- META {
-- META   "language": "sparksql"
-- META }
""", ext="sql")

add_nb(10, "nb_broken_syntax", py_nb([("code", '''df = spark.read.parquet("x"
df.write.saveAsTable("silver_shop.broken_out")''')]))

add_nb(11, "nb_delta_api", py_nb([("code", '''from delta.tables import DeltaTable
tgt = DeltaTable.forName(spark, "silver_shop.orders")
tgt.alias("t").merge(src.alias("s"), "t.id = s.id").whenMatchedUpdateAll().execute()
write_deltalake("abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Tables/gold_shop/export_tbl", df)
existing = DeltaTable("abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Tables/silver_shop/customers")''')]))

add_nb(12, "nb_no_tables", py_nb([("code", '''print("hello")  # SELECT * FROM not_a_table.in_a_comment
x = "FROM fake.table in a plain string"''')]))


add_nb(13, "nb_mlv_orders", py_nb([("sql", """CREATE OR REPLACE MATERIALIZED LAKE VIEW gold_shop.orders_mlv
(
  CONSTRAINT valid_amount CHECK (amount >= 0) ON MISMATCH DROP
)
COMMENT 'orders by region'
AS
SELECT o.order_id, o.amount, c.region
FROM silver_shop.orders o
LEFT JOIN silver_shop.customers c ON o.customer_id = c.customer_id""")]))

add_nb(14, "nb_duckdb_report", py_nb([("code", '''rows = execute_query("""
SELECT region, SUM(amount) AS total
FROM gold_shop.revenue_daily r
JOIN silver_shop.customers c ON r.customer_id = c.customer_id
GROUP BY region
""")
execute_mutation_sql("DELETE FROM gold_shop.report_cache WHERE region IS NOT NULL")''')]))
add_nb(15, "nb_literal_sql", py_nb([("code", '''commands = []
commands.append("DELETE FROM gold_shop.stale_rows WHERE age > 30")
tables_to_check = ["silver_shop.orders", "silver_shop.customers", "df.csv"]
for c in commands:
    spark.sql(c)''')]))
add_nb(16, "nb_four_part", py_nb([("code", '''spark.sql("SELECT * FROM `Contoso WS`.shop_lh.silver_shop.orders")
spark.sql("INSERT INTO shop_lh.gold_shop.four_part_out SELECT * FROM silver_shop.customers")''')]))
add_nb(17, "nb_silver_orders_python", py_nb([
    ("sql", """CREATE OR REPLACE TEMP VIEW v_orders AS
SELECT o.order_id, o.customer_id, o.order_date, o.amount, c.region
FROM bronze_shop.orders o
JOIN bronze_shop.customers c ON o.customer_id = c.customer_id"""),
    ("code", '''df = spark.sql("SELECT * FROM v_orders")
df.write.mode("overwrite").saveAsTable("silver_shop.orders")'''),
]))
add_nb(18, "nb_col_expr", py_nb([("code", '''spark.sql("""
INSERT INTO gold_shop.order_flags (order_id, is_big, label)
SELECT o.order_id,
       CASE WHEN o.amount > 100 THEN 1 ELSE 0 END,
       'x' || c.region
FROM silver_shop.orders o
JOIN silver_shop.customers c ON o.customer_id = c.customer_id
""")''')]))

add_nb(19, "nb_union_events", py_nb([("code", '''spark.sql("""
INSERT INTO gold_shop.all_events
SELECT id, 'a' AS src FROM staging.a
UNION ALL
SELECT id, 'b' AS src FROM staging.b
""")''')]))

add_nb(20, "nb_merge_explicit", py_nb([("code", '''spark.sql("""
MERGE INTO silver_shop.order_totals AS t
USING (SELECT customer_id, SUM(amount) AS total FROM silver_shop.orders GROUP BY customer_id) AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.total_amount = s.total + t.bonus
WHEN NOT MATCHED THEN INSERT (customer_id, total_amount) VALUES (s.customer_id, s.total)
""")''')]))

add_nb(21, "nb_star_unknown", py_nb([("code", '''spark.sql("INSERT INTO gold_shop.copy_unknown SELECT * FROM unknown_schema.t")''')]))
add_nb(22, "nb_silver_loader", py_nb([("code", '''cfg_path = lakehouse_table_path("cfg", "silver_load")
for row in rows:
    dest = row["table_name"]
    spark.sql(row["query"]).write.mode("overwrite").saveAsTable(dest)''')]))

add_nb(23, "nb_custom_silver", py_nb([("code", '''df = spark.sql("SELECT * FROM bronze_shop.orders")
df.write.saveAsTable(target)''')]))
add_nb(24, "nb_param_pipeline", py_nb([
    ("code", '''param_table_name = "silver_shop.param_out"'''),
    ("code", '''target_schema, target_name = param_table_name.split(".", 1)
target = f"{target_schema}.{target_name}"
q = """
SELECT o.order_id, UPPER(c.region) AS region
FROM bronze_shop.orders o
JOIN bronze_shop.customers c ON o.customer_id = c.customer_id
"""
spark.sql(q).createOrReplaceTempView("stg_param")
src = spark.table("stg_param")
src.write.mode("overwrite").saveAsTable(target)'''),
]))

add_nb(25, "nb_chain_write", py_nb([("code", '''spark.sql("SELECT order_id, amount FROM bronze_shop.orders").write.mode("overwrite").saveAsTable("silver_shop.chain_out")''')]))
# SQL passed through a helper function, and statements kept in a literal list and run in a loop
add_nb(27, "nb_sql_helpers", py_nb([("code", '''def run_sql(query, desc=None):
    if desc:
        print(desc)
    return spark.sql(query)

run_sql("INSERT INTO gold_shop.helper_out SELECT order_id, amount FROM silver_shop.orders", "load helper_out")

STEPS = [
    ("orders", "INSERT INTO gold_shop.loop_out SELECT order_id FROM silver_shop.orders"),
    ("customers", "INSERT INTO gold_shop.loop_out SELECT customer_id AS order_id FROM silver_shop.customers"),
]
for label, sql in STEPS:
    spark.sql(sql)''')]))
# a temp view rebuilt from itself with DataFrame unionByName (zero columns in one branch, real values in the other)
add_nb(26, "nb_union_by_name", py_nb([
    ("sql", """CREATE OR REPLACE TEMP VIEW v_adj AS
SELECT order_id, amount FROM silver_shop.orders"""),
    ("code", '''df_base = spark.sql("""
SELECT *, CAST(0 AS DECIMAL(38, 6)) AS adjustment
FROM v_adj
""")
df_extra = spark.sql("""
SELECT a.order_id, CAST(0 AS DECIMAL(38, 6)) AS amount, COALESCE(a.adjustment_value, 0) AS adjustment
FROM staging.adjustments AS a
""")
df_base.unionByName(df_extra).createOrReplaceTempView("v_adj")
spark.sql("SELECT * FROM v_adj").write.mode("overwrite").saveAsTable("gold_shop.orders_adjusted")'''),
]))
def notebook_activity(name, nid, deps=None):
    a = {"name": name, "type": "TridentNotebook",
         "typeProperties": {"notebookId": nid, "workspaceId": "00000000-0000-0000-0000-000000000000"}}
    if deps:
        a["dependsOn"] = [{"activity": d, "dependencyConditions": ["Succeeded"]} for d in deps]
    return a


def add_pl(i, name, activities):
    PL_IDS[name] = gid(1000 + i)
    d = os.path.join(SYN, "pipelines", f"{name}.DataPipeline")
    write(os.path.join(d, ".platform"), platform("DataPipeline", name, gid(1000 + i)))
    write(os.path.join(d, "pipeline-content.json"),
          json.dumps({"properties": {"activities": activities}}, indent=2))


def lh_ds(schema, table):
    return {"type": "LakehouseTable", "typeProperties": {"schema": schema, "table": table}}


add_pl(1, "pl_main", [
    notebook_activity("run_ingest", NB_IDS["nb_ingest_orders"]),
    notebook_activity("run_silver_orders", NB_IDS["nb_silver_orders"], ["run_ingest"]),
    {"name": "call_copy", "type": "InvokePipeline",
     "typeProperties": {"pipelineId": gid(1002), "workspaceId": "00000000-0000-0000-0000-000000000000"}},
    {"name": "for_each_cfg", "type": "ForEach", "typeProperties": {
        "items": {"value": "@pipeline().parameters.items", "type": "Expression"},
        "activities": [notebook_activity("run_gold", NB_IDS["nb_gold_revenue"])]}},
    {"name": "if_full_load", "type": "IfCondition", "typeProperties": {
        "expression": {"value": "@true", "type": "Expression"},
        "ifTrueActivities": [notebook_activity("run_generic", NB_IDS["nb_generic_load"])],
        "ifFalseActivities": []}},
])

add_pl(2, "pl_copy", [
    {"name": "copy_revenue", "type": "Copy", "typeProperties": {
        "source": {"type": "LakehouseTableSource", "datasetSettings": lh_ds("gold_shop", "revenue_daily")},
        "sink": {"type": "LakehouseTableSink", "datasetSettings": lh_ds("dwh_stage", "revenue_daily")}}},
    {"name": "get_cfg", "type": "Lookup", "typeProperties": {
        "source": {"type": "LakehouseTableSource", "sqlReaderQuery": "SELECT id FROM cfg.load_cfg WHERE enabled = 1"}}},
    {"name": "truncate_stage", "type": "Script", "typeProperties": {
        "scripts": [{"type": "NonQuery", "text": "TRUNCATE TABLE dwh_stage.revenue_daily"}]}},
])

add_pl(3, "pl_dynamic", [
    {"name": "copy_dynamic", "type": "Copy", "typeProperties": {
        "source": {"type": "LakehouseTableSource", "datasetSettings": lh_ds("gold_shop", "orders_snapshot")},
        "sink": {"type": "LakehouseTableSink", "datasetSettings": {
            "type": "LakehouseTable", "typeProperties": {"schema": "dwh_stage", "table": {
                "value": "@pipeline().parameters.TargetTable", "type": "Expression"}}}}}},
    notebook_activity("run_unknown", "ffffffff-0000-0000-0000-000000000000"),
])

# semantic model
sm = os.path.join(SYN, "semantic", "Shop Model.SemanticModel")
write(os.path.join(sm, ".platform"), platform("SemanticModel", "Shop Model", gid(2001)))
write(os.path.join(sm, "definition", "tables", "revenue_daily.tmdl"), """table revenue_daily
\tlineageTag: a1

\tmeasure 'Total Revenue' = SUM(revenue_daily[amount])

\tcolumn amount
\t\tsourceColumn: amount

\tpartition revenue_daily = entity
\t\tmode: directLake
\t\tsource
\t\t\tentityName: revenue_daily
\t\t\tschemaName: gold_shop
\t\t\texpressionSource: DatabaseQuery
""")
write(os.path.join(sm, "definition", "tables", "dim_customers.tmdl"), """table 'Dim Customers'
\tlineageTag: a2

\tpartition 'Dim Customers' = entity
\t\tmode: directLake
\t\tsource
\t\t\tentityName: customers
\t\t\tschemaName: silver_shop
\t\t\texpressionSource: DatabaseQuery
""")
write(os.path.join(sm, "definition", "tables", "orders_import.tmdl"), """table orders_import
\tlineageTag: a3

\tpartition orders_import = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database("server", "db"),
\t\t\t\t    t = Source{[Schema="silver_shop",Item="orders"]}[Data]
\t\t\t\tin
\t\t\t\t    t
""")

# config export
write(os.path.join(SYN, "config", "json_files", "load_cfg.json"), json.dumps([
    {"id": 1, "src_name": "SHOP", "src_table": "Orders", "dest_table_schema": "bronze_shop", "dest_table": "orders", "enabled": True},
    {"id": 2, "src_name": "SHOP", "src_table": "Customers", "dest_table_schema": "bronze_shop", "dest_table": "customers", "enabled": True},
    {"id": 3, "src_name": "SHOP", "src_table": "Returns", "dest_table_schema": "bronze_shop", "dest_table": "returns", "enabled": False},
], indent=2))

# schedule: pl_main runs daily (enabled); a second, disabled schedule must not count twice
write(os.path.join(SYN, "pipelines", "pl_main.DataPipeline", ".schedules"), json.dumps({
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/schedules/1.0.0/schema.json",
    "schedules": [
        {"enabled": True, "jobType": "Execute", "configuration": {
            "type": "Daily", "startDateTime": "2026-01-01T00:00:00",
            "endDateTime": "9999-12-31T23:59:59.9999999", "localTimeZoneId": "UTC", "times": ["06:00"]}},
        {"enabled": False, "jobType": "Execute", "configuration": {
            "type": "Daily", "startDateTime": "2026-01-01T00:00:00",
            "endDateTime": "9999-12-31T23:59:59.9999999", "localTimeZoneId": "UTC", "times": ["18:00"]}},
    ]}, indent=2))

# synthetic last-change dates (what `columntrace fetch-dates` would produce)
dates = {"schemaVersion": 1, "source": "synthetic", "branch": "main",
         "fetchedAt": "2026-10-24T00:00:00+00:00", "apiCalls": 0, "items": {}}
default = {"lastChange": "2026-10-01T09:00:00Z", "commit": "aaaa0001"}
for nm in NB_IDS:
    dates["items"][f"notebooks/{nm}.Notebook"] = dict(default)
for nm in PL_IDS:
    dates["items"][f"pipelines/{nm}.DataPipeline"] = dict(default)
dates["items"]["notebooks/nb_silver_orders.Notebook"] = {"lastChange": "2026-01-10T09:00:00Z", "commit": "bbbb0002"}
dates["items"]["notebooks/nb_silver_orders_python.Notebook"] = {"lastChange": "2026-09-01T09:00:00Z", "commit": "cccc0003"}
dates["items"]["notebooks/nb_no_tables.Notebook"] = {"lastChange": "2023-05-01T09:00:00Z", "commit": "dddd0004"}
dates["items"]["notebooks/nb_report_view.Notebook"] = {"lastChange": "2024-02-01T09:00:00Z", "commit": "eeee0005"}
write(os.path.join(ROOT, "fixtures", "synthetic_dates.json"), json.dumps(dates, indent=1))
# hand-labelled ground truth (written independently of the extractor)
N, P, T = "notebook:", "pipeline:", "table:"
truth = {
    "edges": [
        [N + "nb_ingest_orders", T + "staging.orders_raw", "reads"],
        [N + "nb_ingest_orders", T + "bronze_shop.orders", "writes"],
        [N + "nb_silver_orders", T + "bronze_shop.orders", "reads"],
        [N + "nb_silver_orders", T + "bronze_shop.customers", "reads"],
        [N + "nb_silver_orders", T + "silver_shop.orders", "writes"],
        [N + "nb_silver_customers", T + "bronze_shop.customers", "reads"],
        [N + "nb_silver_customers", T + "silver_shop.customers", "writes"],
        [N + "nb_gold_revenue", T + "silver_shop.orders", "reads"],
        [N + "nb_gold_revenue", T + "silver_shop.customers", "reads"],
        [N + "nb_gold_revenue", T + "gold_shop.revenue_daily", "writes"],
        [N + "nb_dynamic_export", None, "reads"],       # unresolved, listed below
        [N + "nb_helper_loader", T + "silver_shop.orders", "reads"],
        [N + "nb_helper_loader", T + "gold_shop.orders_snapshot", "writes"],
        [N + "nb_orchestrator", N + "nb_silver_orders", "calls"],
        [N + "nb_orchestrator", N + "nb_gold_revenue", "calls"],
        [N + "nb_generic_load", T + "cfg.load_cfg", "reads"],
        [N + "nb_generic_load", T + "staging.incoming", "reads"],
        [N + "nb_generic_load", T + "bronze_shop.orders", "writes"],
        [N + "nb_generic_load", T + "bronze_shop.customers", "writes"],
        [N + "nb_generic_load", "source:shop.orders", "reads"],
        [N + "nb_generic_load", "source:shop.customers", "reads"],
        [N + "nb_report_view", T + "gold_shop.revenue_daily", "reads"],
        [N + "nb_report_view", T + "gold_shop.v_revenue", "writes"],
        [N + "nb_broken_syntax", T + "silver_shop.broken_out", "writes"],
        [N + "nb_delta_api", T + "silver_shop.orders", "writes"],
        [N + "nb_delta_api", T + "gold_shop.export_tbl", "writes"],
        [N + "nb_delta_api", T + "silver_shop.customers", "reads"],
        [N + "nb_mlv_orders", T + "gold_shop.orders_mlv", "writes"],
        [N + "nb_mlv_orders", T + "silver_shop.orders", "reads"],
        [N + "nb_mlv_orders", T + "silver_shop.customers", "reads"],
        [N + "nb_duckdb_report", T + "gold_shop.revenue_daily", "reads"],
        [N + "nb_duckdb_report", T + "silver_shop.customers", "reads"],
        [N + "nb_duckdb_report", T + "gold_shop.report_cache", "writes"],
        [N + "nb_literal_sql", T + "gold_shop.stale_rows", "writes"],
        [N + "nb_literal_sql", T + "silver_shop.orders", "references"],
        [N + "nb_literal_sql", T + "silver_shop.customers", "references"],        [N + "nb_four_part", T + "silver_shop.orders", "reads"],
        [N + "nb_four_part", T + "gold_shop.four_part_out", "writes"],
        [N + "nb_four_part", T + "silver_shop.customers", "reads"],        [N + "nb_silver_orders_python", T + "bronze_shop.orders", "reads"],
        [N + "nb_silver_orders_python", T + "bronze_shop.customers", "reads"],
        [N + "nb_silver_orders_python", T + "silver_shop.orders", "writes"],        [N + "nb_col_expr", T + "gold_shop.order_flags", "writes"],
        [N + "nb_col_expr", T + "silver_shop.orders", "reads"],
        [N + "nb_col_expr", T + "silver_shop.customers", "reads"],
        [N + "nb_union_events", T + "gold_shop.all_events", "writes"],
        [N + "nb_union_events", T + "staging.a", "reads"],
        [N + "nb_union_events", T + "staging.b", "reads"],
        [N + "nb_merge_explicit", T + "silver_shop.order_totals", "writes"],
        [N + "nb_merge_explicit", T + "silver_shop.orders", "reads"],
        [N + "nb_star_unknown", T + "gold_shop.copy_unknown", "writes"],
        [N + "nb_star_unknown", T + "unknown_schema.t", "reads"],
        [N + "nb_silver_loader", T + "cfg.silver_load", "reads"],
        [N + "nb_custom_silver", T + "bronze_shop.orders", "reads"],        [N + "nb_param_pipeline", T + "bronze_shop.orders", "reads"],
        [N + "nb_param_pipeline", T + "bronze_shop.customers", "reads"],
        [N + "nb_param_pipeline", T + "silver_shop.param_out", "writes"],
        [N + "nb_chain_write", T + "bronze_shop.orders", "reads"],
        [N + "nb_chain_write", T + "silver_shop.chain_out", "writes"],
        [N + "nb_sql_helpers", T + "silver_shop.orders", "reads"],
        [N + "nb_sql_helpers", T + "silver_shop.customers", "reads"],
        [N + "nb_sql_helpers", T + "gold_shop.helper_out", "writes"],
        [N + "nb_sql_helpers", T + "gold_shop.loop_out", "writes"],
        [N + "nb_union_by_name", T + "silver_shop.orders", "reads"],
        [N + "nb_union_by_name", T + "staging.adjustments", "reads"],
        [N + "nb_union_by_name", T + "gold_shop.orders_adjusted", "writes"],
        [P + "pl_main", N + "nb_ingest_orders", "calls"],
        [P + "pl_main", N + "nb_silver_orders", "calls"],
        [P + "pl_main", P + "pl_copy", "calls"],
        [P + "pl_main", N + "nb_gold_revenue", "calls"],
        [P + "pl_main", N + "nb_generic_load", "calls"],
        [P + "pl_copy", T + "gold_shop.revenue_daily", "reads"],
        [P + "pl_copy", T + "dwh_stage.revenue_daily", "writes"],
        [P + "pl_copy", T + "cfg.load_cfg", "reads"],
        [P + "pl_dynamic", T + "gold_shop.orders_snapshot", "reads"],
        ["semantic_table:Shop Model.revenue_daily", T + "gold_shop.revenue_daily", "binds"],
        ["semantic_table:Shop Model.Dim Customers", T + "silver_shop.customers", "binds"],
        ["semantic_table:Shop Model.orders_import", T + "silver_shop.orders", "binds"],
    ],
    "unresolved": [
        [N + "nb_dynamic_export", "reads", "silver_shop.{t}"],
        [N + "nb_dynamic_export", "writes", "{output_name}"],
        [N + "nb_generic_load", "writes", "{row['dest_schema']}.{row['dest_table']}"],
        [N + "nb_silver_loader", "writes", "{dest}"],
        [N + "nb_custom_silver", "writes", "{target}"],
        [N + "nb_orchestrator", "calls", "notebook:nb_missing_nb"],
        [P + "pl_dynamic", "writes", "dwh_stage.@pipeline().parameters.TargetTable"],
        [P + "pl_dynamic", "calls", "notebook:ffffffff-0000-0000-0000-000000000000"],
    ],
    "forbidden_tables": ["not_a_table.in_a_comment", "fake.table", "v_orders", "latest", "df.csv"],
}
truth["edges"] = [e for e in truth["edges"] if e[1] is not None]
write(os.path.join(ROOT, "fixtures", "ground_truth.json"), json.dumps(truth, indent=1))

# synthetic schema export (what `columntrace export-schema` writes) incl. a table the ignore list must drop
SCHEMA = {
    "staging.orders_raw": ["order_id", "customer_id", "order_date", "amount"],
    "staging.adjustments": ["order_id", "adjustment_value"],
    "gold_shop.helper_out": ["order_id", "amount"],
    "gold_shop.loop_out": ["order_id"],
    "gold_shop.orders_adjusted": ["order_id", "amount", "adjustment"],
    "staging.a": ["id"], "staging.b": ["id"],
    "bronze_shop.orders": ["order_id", "customer_id", "order_date", "amount"],
    "bronze_shop.customers": ["customer_id", "name", "region"],
    "silver_shop.orders": ["order_id", "customer_id", "order_date", "amount", "region"],
    "silver_shop.customers": ["customer_id", "name", "region"],
    "silver_shop.order_totals": ["customer_id", "total_amount", "bonus"],
    "gold_shop.revenue_daily": ["order_date", "amount"],
    "gold_shop.orders_mlv": ["order_id", "amount", "region"],
    "gold_shop.order_flags": ["order_id", "is_big", "label"],
    "gold_shop.all_events": ["id", "src"],
    "sandbox.junk_table": ["x", "y"],
}
write(os.path.join(ROOT, "fixtures", "synthetic_schema.json"), json.dumps({
    "schemaVersion": 1, "source": "synthetic", "exportedAt": "2026-10-24T00:00:00+00:00",
    "counts": {"tables": len(SCHEMA), "userTables": len(SCHEMA), "columns": sum(len(v) for v in SCHEMA.values())},
    "tables": {t: {"columns": [{"name": c, "type": "string", "nullable": True} for c in cols]} for t, cols in SCHEMA.items()},
}, indent=1))

# column ground truth: [to table, to column, from table, from column, transform, confidence]
COLS = [
    ["bronze_shop.orders", c, "staging.orders_raw", c, "direct", "parsed"] for c in ["order_id", "customer_id", "order_date", "amount"]
] + [
    # temp view v_orders + df.write.saveAsTable("silver_shop.orders"), in both the notebook and its _python twin
    ["silver_shop.orders", "order_id", "bronze_shop.orders", "order_id", "direct", "parsed"],
    ["silver_shop.orders", "customer_id", "bronze_shop.orders", "customer_id", "direct", "parsed"],
    ["silver_shop.orders", "order_date", "bronze_shop.orders", "order_date", "direct", "parsed"],
    ["silver_shop.orders", "amount", "bronze_shop.orders", "amount", "direct", "parsed"],
    ["silver_shop.orders", "region", "bronze_shop.customers", "region", "direct", "parsed"],
    # MERGE ... UPDATE SET * / INSERT * through a CTE with SELECT *: matched by name, so inferred
    ["silver_shop.customers", "customer_id", "bronze_shop.customers", "customer_id", "direct", "inferred"],
    ["silver_shop.customers", "name", "bronze_shop.customers", "name", "direct", "inferred"],
    ["silver_shop.customers", "region", "bronze_shop.customers", "region", "direct", "inferred"],
    # INSERT OVERWRITE without a column list; positions agree with the target schema
    ["gold_shop.revenue_daily", "order_date", "silver_shop.orders", "order_date", "direct", "parsed"],
    ["gold_shop.revenue_daily", "amount", "silver_shop.orders", "amount", "aggregate", "parsed"],
    # materialized lake view, constraints and comment stripped
    ["gold_shop.orders_mlv", "order_id", "silver_shop.orders", "order_id", "direct", "parsed"],
    ["gold_shop.orders_mlv", "amount", "silver_shop.orders", "amount", "direct", "parsed"],
    ["gold_shop.orders_mlv", "region", "silver_shop.customers", "region", "direct", "parsed"],
    # explicit column list with expressions; the constant 'x' prefix is not a source column
    ["gold_shop.order_flags", "order_id", "silver_shop.orders", "order_id", "direct", "parsed"],
    ["gold_shop.order_flags", "is_big", "silver_shop.orders", "amount", "expression", "parsed"],
    ["gold_shop.order_flags", "label", "silver_shop.customers", "region", "expression", "parsed"],
    # UNION ALL: one output column fed by both branches
    ["gold_shop.all_events", "id", "staging.a", "id", "direct", "parsed"],
    ["gold_shop.all_events", "id", "staging.b", "id", "direct", "parsed"],
    # MERGE with explicit assignments; t.bonus is the target itself and is not a source
    ["silver_shop.order_totals", "customer_id", "silver_shop.orders", "customer_id", "direct", "parsed"],
    ["silver_shop.order_totals", "total_amount", "silver_shop.orders", "amount", "aggregate", "parsed"],   # s.total + t.bonus, s.total = SUM(amount)
    # four-part name target, SELECT * expanded through the schema
    ["gold_shop.four_part_out", "customer_id", "silver_shop.customers", "customer_id", "direct", "parsed"],
    ["gold_shop.four_part_out", "name", "silver_shop.customers", "name", "direct", "parsed"],
    ["gold_shop.four_part_out", "region", "silver_shop.customers", "region", "direct", "parsed"],
    # CREATE VIEW ... AS SELECT * (a .sql notebook)
    ["gold_shop.v_revenue", "order_date", "gold_shop.revenue_daily", "order_date", "direct", "parsed"],
    ["gold_shop.v_revenue", "amount", "gold_shop.revenue_daily", "amount", "direct", "parsed"],
    # parameter default + split() unpack -> target name; spark.sql(q) -> temp view -> spark.table -> saveAsTable
    ["silver_shop.param_out", "order_id", "bronze_shop.orders", "order_id", "direct", "parsed"],
    ["silver_shop.param_out", "region", "bronze_shop.customers", "region", "expression", "parsed"],
    # spark.sql(...).write.saveAsTable(...) in one chained expression
    ["silver_shop.chain_out", "order_id", "bronze_shop.orders", "order_id", "direct", "parsed"],
    ["silver_shop.chain_out", "amount", "bronze_shop.orders", "amount", "direct", "parsed"],
    # SQL handed to a helper function, and a loop over a literal list of statements
    ["gold_shop.helper_out", "order_id", "silver_shop.orders", "order_id", "direct", "parsed"],
    ["gold_shop.helper_out", "amount", "silver_shop.orders", "amount", "direct", "parsed"],
    ["gold_shop.loop_out", "order_id", "silver_shop.orders", "order_id", "direct", "parsed"],
    ["gold_shop.loop_out", "order_id", "silver_shop.customers", "customer_id", "direct", "parsed"],
    # unionByName rebuilds v_adj from itself: each column from the branch that names it
    ["gold_shop.orders_adjusted", "order_id", "silver_shop.orders", "order_id", "direct", "parsed"],
    ["gold_shop.orders_adjusted", "order_id", "staging.adjustments", "order_id", "direct", "parsed"],
    ["gold_shop.orders_adjusted", "amount", "silver_shop.orders", "amount", "direct", "parsed"],
    ["gold_shop.orders_adjusted", "adjustment", "staging.adjustments", "adjustment_value", "expression", "parsed"],
    # SELECT * from a table with no known schema stays an unresolved star
    ["gold_shop.copy_unknown", "*", "unknown_schema.t", "*", "star", "inferred"],
]
write(os.path.join(ROOT, "fixtures", "ground_truth_columns.json"), json.dumps({"edges": COLS}, indent=1))

# synthetic config table export (what `columntrace export-table` writes): rows map a target table
# to a SELECT, or to a notebook that fills it
CFG_ROWS = [
    {"id": 1, "table_name": "silver_shop.orders_cfg", "enabled": True, "is_notebook": False, "except_columns": "name",
     "source_pk": "order_id", "target_pk": "order_id",
     "query": "SELECT o.order_id, c.region AS region, c.name AS name FROM bronze_shop.orders o JOIN bronze_shop.customers c ON o.customer_id = c.customer_id"},
    {"id": 2, "table_name": "silver_shop.custom_out", "enabled": True, "is_notebook": True, "except_columns": None,
     "source_pk": None, "target_pk": None, "query": "nb_custom_silver"},
    {"id": 3, "table_name": "silver_shop.disabled_out", "enabled": False, "is_notebook": False, "except_columns": None,
     "source_pk": "id", "target_pk": "id", "query": "SELECT * FROM bronze_shop.orders"},
    {"id": 4, "table_name": "silver_shop.totals_cfg", "enabled": True, "is_notebook": False, "except_columns": None,
     "source_pk": "customer_id", "target_pk": "customer_id",
     "query": "WITH t AS (SELECT customer_id, SUM(amount) AS total FROM silver_shop.orders GROUP BY customer_id) SELECT customer_id, total FROM t"},
]
write(os.path.join(ROOT, "fixtures", "synthetic_config_table.json"), json.dumps({
    "schemaVersion": 1, "source": "sql-endpoint-table", "table": "cfg.silver_load", "truncated": False,
    "exportedAt": "2026-10-24T00:00:00+00:00", "columns": list(CFG_ROWS[0].keys()), "rows": CFG_ROWS}, indent=1))
print("fixtures written:", len(NB_IDS), "notebooks,", len(PL_IDS), "pipelines")

# the sample the UI opens by default: the synthetic workspace extracted with every input, on a
# fixed clock so the file only changes when the extractor or the fixtures do
sys.path.insert(0, os.path.join(ROOT, "src"))
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from columntrace.extract import describe_inputs, extract  # noqa: E402
from columntrace.gitdates import load_dates  # noqa: E402
from columntrace.schema_export import load_schema  # noqa: E402

FX = os.path.join(ROOT, "fixtures")
sample = extract(Path(FX, "synthetic_workspace"), dates=load_dates(os.path.join(FX, "synthetic_dates.json")),
                 now=datetime(2026, 10, 24, tzinfo=UTC), schema=load_schema(os.path.join(FX, "synthetic_schema.json")),
                 config_tables=[os.path.join(FX, "synthetic_config_table.json")],
                 inputs=describe_inputs(None, os.path.join(FX, "synthetic_dates.json"), os.path.join(FX, "synthetic_schema.json"),
                                        [os.path.join(FX, "synthetic_config_table.json")]))
write(os.path.join(ROOT, "ui", "public", "sample-lineage.json"), json.dumps(sample.to_dict(), indent=1))
print("sample written: ui/public/sample-lineage.json")
