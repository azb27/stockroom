-- core.* : the cleaned semantic layer the agent queries.
-- Every rule below detects its issue from raw.* alone. It never reads the dirt manifest.
-- Each fix is logged to core.dq_issues so the agent (and the customer) can see what was changed.

CREATE SCHEMA IF NOT EXISTS core;

-- 1. SKU code normalisation (D1) -------------------------------------------------------
CREATE OR REPLACE TABLE core.sku_alias AS
WITH codes AS (
    SELECT sku_code AS raw_code FROM raw.pos_sales_lines
    UNION SELECT sku_code FROM raw.erp_sku_master
    UNION SELECT sku_code FROM raw.inventory_snapshot
    UNION SELECT sku_code FROM raw.price_book
)
SELECT raw_code,
       upper(trim(replace(raw_code, '-', '_'))) AS sku,
       raw_code <> upper(trim(replace(raw_code, '-', '_'))) AS is_alias
FROM codes;

-- 2. SKU dimension, with case_pack imputed from department mode (D7) --------------------
CREATE OR REPLACE TABLE core.dim_sku AS
WITH m AS (
    SELECT * FROM raw.erp_sku_master
    WHERE sku_code = upper(trim(replace(sku_code, '-', '_')))
), dept_mode AS (
    SELECT dept, mode(case_pack) AS cp FROM m WHERE case_pack > 0 GROUP BY dept
)
SELECT m.sku_code AS sku, m.description, m.dept, m.category,
       CASE WHEN m.case_pack > 0 THEN m.case_pack ELSE dept_mode.cp END AS case_pack,
       NOT coalesce(m.case_pack > 0, false) AS case_pack_imputed,
       m.unit_cost, m.supplier_id, m.status
FROM m JOIN dept_mode USING (dept);

CREATE OR REPLACE TABLE core.dim_supplier AS SELECT * FROM raw.erp_suppliers;
CREATE OR REPLACE TABLE core.dim_store AS SELECT store_code AS store, account_name FROM raw.stores;
CREATE OR REPLACE TABLE core.dim_date AS SELECT * FROM raw.calendar;
-- known-in-advance calendar (events, SNAP) for the forecast horizon; no sales exist for these dates
CREATE OR REPLACE TABLE core.dim_date_future AS SELECT * FROM raw.calendar_future;

-- 3. Load de-duplication (D3): keep the first load of each source file --------------------
CREATE OR REPLACE TABLE core.load_batches AS
SELECT *, row_number() OVER (PARTITION BY source_file ORDER BY loaded_at, load_batch_id) AS load_rank
FROM raw.load_log;

-- 4. Daily sales fact: dedupe + UoM conversion (D2) + alias resolution (D1) --------------
CREATE OR REPLACE TABLE core.fact_sales_daily AS
SELECT l.txn_date AS date, l.store_code AS store, a.sku,
       CAST(sum(round(CASE WHEN upper(trim(l.uom)) IN ('CS', 'CASE') THEN l.qty * d.case_pack
                           ELSE l.qty END)) AS INTEGER) AS units
FROM raw.pos_sales_lines l
JOIN core.load_batches b ON b.load_batch_id = l.load_batch_id AND b.load_rank = 1
JOIN core.sku_alias a ON a.raw_code = l.sku_code
JOIN core.dim_sku d ON d.sku = a.sku
GROUP BY ALL;

-- 5. Store-day status (D4): distinguish an outage from a chain-wide closure ---------------
-- A store-day is "thin" if it has < 5% of that store's median daily line count.
-- If every store is thin that day it is a closure (e.g. Christmas); otherwise, missing data.
CREATE OR REPLACE TABLE core.store_day_status AS
WITH grid AS (
    SELECT c.date, s.store FROM core.dim_date c CROSS JOIN core.dim_store s
), act AS (
    SELECT date, store, count(*) AS n_lines, sum(units) AS units FROM core.fact_sales_daily GROUP BY ALL
), j AS (
    SELECT grid.date, grid.store, coalesce(act.n_lines, 0) AS n_lines, coalesce(act.units, 0) AS units
    FROM grid LEFT JOIN act USING (date, store)
), med AS (
    SELECT store, median(n_lines) AS med_lines FROM j GROUP BY store
), flagged AS (
    SELECT j.*, j.n_lines < 0.05 * med.med_lines AS thin FROM j JOIN med USING (store)
), day AS (
    SELECT date, bool_and(thin) AS all_thin FROM flagged GROUP BY date
)
SELECT f.date, f.store, f.n_lines, f.units,
       CASE WHEN NOT f.thin THEN 'open'
            WHEN day.all_thin THEN 'closed_all_stores'
            ELSE 'missing_data' END AS status
FROM flagged f JOIN day USING (date);

-- 6. Weekly prices: unit-error correction (D5) + gap imputation (D6) ---------------------
CREATE OR REPLACE TABLE core.fact_price_weekly AS
WITH p AS (
    SELECT pb.store_code AS store, a.sku, pb.wm_yr_wk, pb.price
    FROM raw.price_book pb JOIN core.sku_alias a ON a.raw_code = pb.sku_code
), med AS (
    SELECT store, sku, median(price) AS med FROM p GROUP BY ALL
), corr AS (
    SELECT p.store, p.sku, p.wm_yr_wk,
           CASE WHEN p.price > 20 * med.med THEN round(p.price / 100, 2) ELSE p.price END AS price,
           p.price > 20 * med.med AS was_corrected
    FROM p JOIN med USING (store, sku)
), last_sale_week AS (
    SELECT s.store, s.sku, max(d.wm_yr_wk) AS wk
    FROM core.fact_sales_daily s JOIN core.dim_date d USING (date) GROUP BY ALL
), bounds AS (
    -- extend to the last week with sales, so a missing final price-book row is still filled
    SELECT c.store, c.sku, min(c.wm_yr_wk) AS w0, greatest(max(c.wm_yr_wk), max(l.wk)) AS w1
    FROM corr c LEFT JOIN last_sale_week l USING (store, sku) GROUP BY ALL
), weeks AS (
    SELECT DISTINCT wm_yr_wk FROM core.dim_date
), grid AS (
    SELECT b.store, b.sku, w.wm_yr_wk FROM bounds b JOIN weeks w ON w.wm_yr_wk BETWEEN b.w0 AND b.w1
), g AS (
    SELECT grid.*, corr.price, corr.was_corrected
    FROM grid LEFT JOIN corr USING (store, sku, wm_yr_wk)
)
SELECT store, sku, wm_yr_wk,
       coalesce(price, last_value(price IGNORE NULLS) OVER (
           PARTITION BY store, sku ORDER BY wm_yr_wk ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       )) AS price,
       price IS NULL AS is_imputed,
       coalesce(was_corrected, false) AS was_corrected
FROM g;

-- 6b. Convenience view: daily sales with price and revenue (the view most questions need) --------
CREATE OR REPLACE VIEW core.sales_enriched AS
SELECT s.date, d.wm_yr_wk, s.store, s.sku, k.dept, k.category, s.units,
       p.price, round(s.units * p.price, 2) AS revenue, p.is_imputed AS price_is_imputed
FROM core.fact_sales_daily s
JOIN core.dim_date d USING (date)
JOIN core.dim_sku k USING (sku)
LEFT JOIN core.fact_price_weekly p ON p.store = s.store AND p.sku = s.sku AND p.wm_yr_wk = d.wm_yr_wk;

-- 7. Inventory (alias-resolved) -------------------------------------------------------------
CREATE OR REPLACE TABLE core.inventory AS
SELECT i.as_of_date, i.store_code AS store, a.sku,
       CAST(sum(i.on_hand) AS INTEGER) AS on_hand_units,
       CAST(sum(i.on_order) AS INTEGER) AS on_order_units
FROM raw.inventory_snapshot i JOIN core.sku_alias a ON a.raw_code = i.sku_code
GROUP BY ALL;

-- 8. Data-quality log: what the pipeline found and did ----------------------------------------
CREATE OR REPLACE TABLE core.dq_issues AS
SELECT 'sku_alias' AS issue_type, 'raw.pos_sales_lines' AS source_table,
       (SELECT count(*) FROM raw.pos_sales_lines l JOIN core.sku_alias a ON a.raw_code = l.sku_code WHERE a.is_alias) AS rows_affected,
       (SELECT count(*) FROM core.sku_alias WHERE is_alias) || ' non-canonical SKU codes (migration re-codes, lowercase, stray whitespace)' AS finding,
       'Normalised to canonical code: upper(trim(replace(code, ''-'', ''_'')))' AS fix_applied
UNION ALL
SELECT 'uom_cases', 'raw.pos_sales_lines',
       (SELECT count(*) FROM raw.pos_sales_lines WHERE upper(trim(uom)) IN ('CS', 'CASE')),
       'Sales lines recorded in cases (labels: ' || (SELECT string_agg(DISTINCT uom, ', ') FROM raw.pos_sales_lines WHERE uom <> 'EA') || ')',
       'Converted to eaches using dim_sku.case_pack'
UNION ALL
SELECT 'duplicate_load', 'raw.load_log',
       (SELECT count(*) FROM raw.pos_sales_lines l JOIN core.load_batches b USING (load_batch_id) WHERE b.load_rank > 1),
       (SELECT string_agg(source_file, ', ') FROM core.load_batches WHERE load_rank > 1) || ' loaded more than once',
       'Kept first load of each source file; later loads excluded'
UNION ALL
SELECT 'missing_store_days', 'raw.pos_sales_lines',
       (SELECT count(*) FROM core.store_day_status WHERE status = 'missing_data'),
       (SELECT string_agg(store || ' ' || date, ', ' ORDER BY date) FROM core.store_day_status WHERE status = 'missing_data') || ' have (almost) no sales while other stores traded',
       'Flagged status = missing_data in core.store_day_status; NOT imputed as zero'
UNION ALL
SELECT 'chain_closure', 'raw.pos_sales_lines',
       (SELECT count(*) FROM core.store_day_status WHERE status = 'closed_all_stores'),
       (SELECT string_agg(DISTINCT CAST(date AS VARCHAR), ', ') FROM core.store_day_status WHERE status = 'closed_all_stores') || ': all stores near-zero (expected closure, not an error)',
       'Flagged status = closed_all_stores; informational'
UNION ALL
SELECT 'price_unit_error', 'raw.price_book',
       (SELECT count(*) FROM core.fact_price_weekly WHERE was_corrected),
       'Prices > 20x the store-SKU median (entered in cents)',
       'Divided by 100; flagged was_corrected'
UNION ALL
SELECT 'price_gap', 'raw.price_book',
       (SELECT count(*) FROM core.fact_price_weekly WHERE is_imputed),
       'Weeks with no price-book row inside a SKU''s active price range',
       'Forward-filled from the previous week; flagged is_imputed'
UNION ALL
SELECT 'case_pack_missing', 'raw.erp_sku_master',
       (SELECT count(*) FROM core.dim_sku WHERE case_pack_imputed),
       'SKU master rows with case_pack NULL or 0',
       'Imputed with the department''s most common case pack; flagged case_pack_imputed';
