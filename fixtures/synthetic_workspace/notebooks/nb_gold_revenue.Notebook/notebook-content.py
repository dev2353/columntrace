# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

SCHEMA = "gold_shop"
TARGET = f"{SCHEMA}.revenue_daily"
query = f"""
INSERT OVERWRITE TABLE {TARGET}
SELECT o.order_date, SUM(o.amount) AS amount
FROM silver_shop.orders o
JOIN silver_shop.customers c ON o.customer_id = c.customer_id
GROUP BY o.order_date
"""
spark.sql(query)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
