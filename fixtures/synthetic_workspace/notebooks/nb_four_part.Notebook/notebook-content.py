# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("SELECT * FROM `Contoso WS`.shop_lh.silver_shop.orders")
spark.sql("INSERT INTO shop_lh.gold_shop.four_part_out SELECT * FROM silver_shop.customers")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
