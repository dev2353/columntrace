# Fabric notebook source

# MARKDOWN ********************

# Silver orders

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# MAGIC %%sql
# MAGIC CREATE OR REPLACE TEMP VIEW v_orders AS
# MAGIC SELECT o.order_id, o.customer_id, o.order_date, o.amount, c.region
# MAGIC FROM bronze_shop.orders o
# MAGIC JOIN bronze_shop.customers c ON o.customer_id = c.customer_id

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

df = spark.sql("SELECT * FROM v_orders")
df.write.mode("overwrite").saveAsTable("silver_shop.orders")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
