\set ON_ERROR_STOP on
begin;

-- BEFORE snapshot
select 'BEFORE' as phase, (select count(*) from work) as work,
       (select count(*) from daily_progress) as daily_progress,
       (select count(*) from baseline_schedule) as baseline_schedule,
       (select max(date) from daily_progress) as max_date;

-- 1. daily_progress: restore ONLY excel_import rows, leave web_form/schedule_derived untouched
delete from daily_progress where source = 'excel_import';

insert into daily_progress overriding system value
select * from restore_stage.daily_progress where source = 'excel_import';

-- 2. baseline_schedule: whole table is excel-derived, safe full replace
delete from baseline_schedule;
insert into baseline_schedule overriding system value
select * from restore_stage.baseline_schedule;

-- 3. work: delete the 2 minted rows (verified zero references in any FK-dependent table)
delete from work where code in ('TM35-AUX-151', 'TM35-AUX-152');

-- 4. work: restore columns of the remaining 169 rows to backup values, in place (no delete/insert -> no FK risk)
update work w set
    code = s.code, source = s.source, location = s.location, name = s.name, unit = s.unit,
    volume = s.volume, weight = s.weight, work_type = s.work_type, executor_type = s.executor_type,
    responsible_id = s.responsible_id, subcontractor_id = s.subcontractor_id, status = s.status,
    criticality = s.criticality, comment = s.comment, section = s.section,
    source_row_ref = s.source_row_ref, fact_pct = s.fact_pct, fact_pct_raw = s.fact_pct_raw,
    data_quality_flag = s.data_quality_flag, data_quality_note = s.data_quality_note,
    created_at = s.created_at, updated_at = s.updated_at, volume_raw = s.volume_raw,
    gesn_norm_id = s.gesn_norm_id, ssr_norm_id = s.ssr_norm_id, plan_finish_date = s.plan_finish_date,
    id_package_id = s.id_package_id, change_id = s.change_id, prescription_id = s.prescription_id,
    amount_rub_with_vat = s.amount_rub_with_vat
from restore_stage.work s
where w.id = s.id;

-- AFTER snapshot
select 'AFTER' as phase, (select count(*) from work) as work,
       (select count(*) from daily_progress) as daily_progress,
       (select count(*) from baseline_schedule) as baseline_schedule,
       (select max(date) from daily_progress) as max_date;

select 'by_source' as check, source, count(*) from daily_progress group by source order by 1;

select 'excel_import_after_oct31' as check, count(*) from daily_progress
where source = 'excel_import' and date > '2026-10-31';

select 'work_count_check' as check, count(*) as should_be_169 from work;

commit;
