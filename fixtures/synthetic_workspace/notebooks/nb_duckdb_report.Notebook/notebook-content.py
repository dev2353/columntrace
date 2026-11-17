# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

rows = execute_query("""
SELECT region, SUM(amount) AS total
FROM gold_shop.revenue_daily r
JOIN silver_shop.customers c ON r.customer_id = c.customer_id
GROUP BY region
""")
execute_mutation_sql("DELETE FROM gold_shop.report_cache WHERE region IS NOT NULL")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
