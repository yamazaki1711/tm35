-- Диагностика природы календарной матрицы (docs/MATRIX_DIAGNOSTICS.md,
-- 15.08.2026): матрица "План" по дням ведёт себя как график работ
-- (matrix_schedule), не как ресурсная разнарядка — 5 тестов на реальных
-- данных, все согласованно "за график". baseline_schedule.plan_start/
-- plan_finish теперь может выводиться из первого/последнего дня с
-- planned_crew > 0 (точнее, дневная точность) вместо текстового поля
-- "Месяц выполнения работ" (месячная точность) — но только для работ,
-- где календарные данные есть; для остальных остаётся текстовый
-- источник как менее точный резерв. Источник и уверенность — явно в
-- БД, не спрятаны в рассуждениях.

begin;

create type baseline_source as enum ('matrix_schedule', 'text_month_only', 'no_data');
create type baseline_confidence as enum ('high', 'medium', 'low', 'none');

alter table baseline_schedule add column baseline_source baseline_source;
alter table baseline_schedule add column confidence baseline_confidence;

commit;
