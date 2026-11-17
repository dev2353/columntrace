# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

cfg_path = lakehouse_table_path("cfg", "load_cfg")
for row in rows:
    target = f"{row['dest_schema']}.{row['dest_table']}"
    spark.sql(f"INSERT INTO {target} SELECT * FROM staging.incoming")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
