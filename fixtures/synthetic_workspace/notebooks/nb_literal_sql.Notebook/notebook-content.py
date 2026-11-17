# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

commands = []
commands.append("DELETE FROM gold_shop.stale_rows WHERE age > 30")
tables_to_check = ["silver_shop.orders", "silver_shop.customers", "df.csv"]
for c in commands:
    spark.sql(c)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
