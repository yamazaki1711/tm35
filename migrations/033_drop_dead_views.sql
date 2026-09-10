-- Заход 3, 10.09.2026, задача 2. Решение координатора: удалить обе
-- неиспользуемые VIEW, найденные при сверке ИД/РСК контуров
-- (KNOWN_ISSUES.md §33). Обоснование: правило "не чистить" защищает
-- данные (таблицы/колонки с настоящими значениями), не производный
-- объект с нулём ссылок, читающий поле, которому приложение само
-- перестало доверять (change.overdue_days, write-only с 08.09.2026).
-- Определения обеих VIEW остаются восстановимыми из истории git
-- (main.py до этого коммита не хранил их текст, но он записан в
-- KNOWN_ISSUES.md §33 и в журнале docs/RUN_3_20260910.md).
--
-- Проверено перед удалением (docs/RUN_3_20260910.md, задача 2):
-- ноль упоминаний в backend/ и tools/, ноль зависимых объектов в БД
-- (pg_depend по обеим VIEW пуст).
--
-- Определения на момент удаления (pg_get_viewdef, для восстановления
-- при необходимости — сама VIEW этой миграцией удаляется):
--
-- create view v_blocked_amounts as
--  SELECT 'CHANGE'::text AS category,
--     change.code,
--     change.topic AS description,
--     change.blocked_amount_rub,
--     change.overdue_days,
--     change.escalation_level
--    FROM change
--   WHERE (change.status <> ALL (ARRAY['INCLUDED_IN_RD'::text, 'ARCHIVED'::text])) AND change.blocked_amount_rub > 0::numeric
-- UNION ALL
--  SELECT 'PRESCRIPTION'::text AS category,
--     prescription.code,
--     prescription.description,
--     prescription.amount_unblocked AS blocked_amount_rub,
--     NULL::integer AS overdue_days,
--     NULL::integer AS escalation_level
--    FROM prescription
--   WHERE prescription.status = 'OPEN'::text AND prescription.amount_unblocked > 0::numeric
--   ORDER BY 4 DESC NULLS LAST;
--
-- create view v_id_dashboard as
--  SELECT status_code AS status,
--     count(*) AS packages_count,
--     sum(COALESCE(amount_no_vat, 0::numeric) * 1.20) AS amount_rub_with_vat,
--     round(100.0 * count(*) FILTER (WHERE date_s60_signed IS NOT NULL)::numeric / NULLIF(count(*), 0)::numeric, 1) AS pct_signed
--    FROM id_package
--   GROUP BY status_code
--   ORDER BY status_code;
begin;

drop view if exists v_blocked_amounts;
drop view if exists v_id_dashboard;

commit;
