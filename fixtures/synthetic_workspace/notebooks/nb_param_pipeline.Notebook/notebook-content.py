# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

param_table_name = "silver_shop.param_out"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

target_schema, target_name = param_table_name.split(".", 1)
target = f"{target_schema}.{target_name}"
q = """
SELECT o.order_id, UPPER(c.region) AS region
FROM bronze_shop.orders o
JOIN bronze_shop.customers c ON o.customer_id = c.customer_id
"""
spark.sql(q).createOrReplaceTempView("stg_param")
src = spark.table("stg_param")
src.write.mode("overwrite").saveAsTable(target)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
