# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

tables = ["orders", "customers"]
for t in tables:
    df = spark.table(f"silver_shop.{t}")
    df.write.saveAsTable(output_name)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
