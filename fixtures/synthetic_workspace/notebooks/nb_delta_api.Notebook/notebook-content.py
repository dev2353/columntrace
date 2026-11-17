# Fabric notebook source

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from delta.tables import DeltaTable
tgt = DeltaTable.forName(spark, "silver_shop.orders")
tgt.alias("t").merge(src.alias("s"), "t.id = s.id").whenMatchedUpdateAll().execute()
write_deltalake("abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Tables/gold_shop/export_tbl", df)
existing = DeltaTable("abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Tables/silver_shop/customers")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
