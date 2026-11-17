# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# MAGIC %%sql
# MAGIC CREATE OR REPLACE TEMP VIEW v_adj AS
# MAGIC SELECT order_id, amount FROM silver_shop.orders

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

df_base = spark.sql("""
SELECT *, CAST(0 AS DECIMAL(38, 6)) AS adjustment
FROM v_adj
""")
df_extra = spark.sql("""
SELECT a.order_id, CAST(0 AS DECIMAL(38, 6)) AS amount, COALESCE(a.adjustment_value, 0) AS adjustment
FROM staging.adjustments AS a
""")
df_base.unionByName(df_extra).createOrReplaceTempView("v_adj")
spark.sql("SELECT * FROM v_adj").write.mode("overwrite").saveAsTable("gold_shop.orders_adjusted")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
