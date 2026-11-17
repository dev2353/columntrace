# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
INSERT INTO gold_shop.order_flags (order_id, is_big, label)
SELECT o.order_id,
       CASE WHEN o.amount > 100 THEN 1 ELSE 0 END,
       'x' || c.region
FROM silver_shop.orders o
JOIN silver_shop.customers c ON o.customer_id = c.customer_id
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
