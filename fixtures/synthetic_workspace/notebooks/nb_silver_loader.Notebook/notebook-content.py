# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

cfg_path = lakehouse_table_path("cfg", "silver_load")
for row in rows:
    dest = row["table_name"]
    spark.sql(row["query"]).write.mode("overwrite").saveAsTable(dest)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
