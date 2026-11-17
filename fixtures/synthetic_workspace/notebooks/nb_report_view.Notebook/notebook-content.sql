-- Fabric notebook source

-- METADATA ********************

-- META {
-- META   "language": "sparksql"
-- META }

-- CELL ********************

CREATE OR REPLACE VIEW gold_shop.v_revenue AS
SELECT * FROM gold_shop.revenue_daily;

-- METADATA ********************

-- META {
-- META   "language": "sparksql"
-- META }
