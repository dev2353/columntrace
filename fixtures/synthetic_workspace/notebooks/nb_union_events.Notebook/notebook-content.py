# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
INSERT INTO gold_shop.all_events
SELECT id, 'a' AS src FROM staging.a
UNION ALL
SELECT id, 'b' AS src FROM staging.b
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
