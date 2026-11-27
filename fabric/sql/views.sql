-- Reporting views on the SQL analytics endpoint of the reporting lakehouse. Synthetic data; footprints and values
-- are illustrative. python -m dbp_mvp report publish runs this after status_flow; a failure here is only a warning.
-- status_flow computes every measure with data/metrics.py; these views only select and label them.

CREATE OR ALTER VIEW dbo.v_passport_status_current AS
SELECT battery_id, model_id, plant_id, batch_id, chemistry, rated_kwh, eligibility, hold_reason, lifecycle_status,
       committed_version, committed_utc, snapshot_state, footprint_per_kwh, footprint_verified, recycled_co_pct,
       recycled_li_pct, recycled_ni_pct, successor_id, second_life_value_eur_illustrative, registry_status,
       data_label, export_id, generated_utc,
       CASE WHEN COALESCE(committed_version, 0) > 0 THEN 1 ELSE 0 END AS is_issued
FROM dbo.passport_status_current;
GO

CREATE OR ALTER VIEW dbo.v_lot_claim_status AS
SELECT lot_id, supplier_id, material, claim_status, case_state, fields_complete, data_label, export_id,
       generated_utc
FROM dbo.lot_claim_status_current;
GO

CREATE OR ALTER VIEW dbo.v_measures_by_plant AS
SELECT plant_id, produced, published, coverage_pct, verified_footprint_batteries, avg_footprint_per_kwh,
       second_life_count, second_life_value_eur_illustrative, data_label, export_id, generated_utc
FROM dbo.measures_by_plant
UNION ALL
SELECT 'ALL', produced, published, coverage_pct, verified_footprint_batteries, avg_kg_co2e_per_kwh,
       second_life_count, second_life_value_eur_illustrative, data_label, export_id, generated_utc
FROM dbo.success_measures;
GO

CREATE OR ALTER VIEW dbo.v_supplier_completeness AS
SELECT supplier_id, material, lots, complete, complete_pct, data_label, export_id, generated_utc
FROM dbo.supplier_completeness;
GO

CREATE OR ALTER VIEW dbo.v_success_measures AS
SELECT produced, published, coverage_pct, verified_footprint_batteries, verified_pct, avg_kg_co2e_per_kwh, lots,
       complete_lots, supplier_completeness_pct, second_life_count, second_life_value_eur_illustrative,
       not_in_export, data_label, export_id, generated_utc
FROM dbo.success_measures;
GO
