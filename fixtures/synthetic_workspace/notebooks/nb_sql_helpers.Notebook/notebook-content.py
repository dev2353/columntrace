# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

def run_sql(query, desc=None):
    if desc:
        print(desc)
    return spark.sql(query)

run_sql("INSERT INTO gold_shop.helper_out SELECT order_id, amount FROM silver_shop.orders", "load helper_out")

STEPS = [
    ("orders", "INSERT INTO gold_shop.loop_out SELECT order_id FROM silver_shop.orders"),
    ("customers", "INSERT INTO gold_shop.loop_out SELECT customer_id AS order_id FROM silver_shop.customers"),
]
for label, sql in STEPS:
    spark.sql(sql)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
