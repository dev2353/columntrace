# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# MAGIC %%sql
# MAGIC CREATE OR REPLACE MATERIALIZED LAKE VIEW gold_shop.orders_mlv
# MAGIC (
# MAGIC   CONSTRAINT valid_amount CHECK (amount >= 0) ON MISMATCH DROP
# MAGIC )
# MAGIC COMMENT 'orders by region'
# MAGIC AS
# MAGIC SELECT o.order_id, o.amount, c.region
# MAGIC FROM silver_shop.orders o
# MAGIC LEFT JOIN silver_shop.customers c ON o.customer_id = c.customer_id

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
