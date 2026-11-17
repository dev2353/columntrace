# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
MERGE INTO silver_shop.order_totals AS t
USING (SELECT customer_id, SUM(amount) AS total FROM silver_shop.orders GROUP BY customer_id) AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.total_amount = s.total + t.bonus
WHEN NOT MATCHED THEN INSERT (customer_id, total_amount) VALUES (s.customer_id, s.total)
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
