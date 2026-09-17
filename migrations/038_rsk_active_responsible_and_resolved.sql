-- ТЗ Якименко А.И. (ПТО), 16.09.2026 — задача 2 (РСК).
--
-- 1) `rsk_responsible.active` — «ИКС» больше не выбирается ни в форме
--    отработки, ни в фильтре реестра (11 записей `rsk_processing_responsible`
--    уже на неё ссылаются, см. run log — строку из справочника не удаляем,
--    только скрываем из списков выбора).
-- 2) `Стройка ТМ-35` → `строй-площадка` — одна правка в справочнике,
--    не три подмены строк по шаблонам.
-- 3) `rsk_processing.resolved`/`resolved_date` — слой 2, отметка ПТО
--    «Устранено». НЕ трогает `rsk_violation.is_active`/`closed_in_act_id` —
--    те остаются структурными, выставляются только импортом акта (правило
--    двух слоёв, CLAUDE.md/докс координатора 06.09.2026, не меняется).

begin;

alter table rsk_responsible add column active boolean not null default true;
update rsk_responsible set active = false where name = 'ИКС';
update rsk_responsible set name = 'строй-площадка' where name = 'Стройка ТМ-35';

alter table rsk_processing add column resolved boolean not null default false;
alter table rsk_processing add column resolved_date date;

-- 4) `rsk_close_condition.label` — слово «неделя» убрано из всех пяти
--    подписей (смысл сохранён), заодно снята попутная аббревиатура
--    «Стр.площадкой» (сама строка правится этой же миграцией — CLAUDE.md
--    запрещает сокращения в интерфейсе, не отдельная задача). До/после —
--    в run log.
update rsk_close_condition set label = 'После принятия РСК соответствующих разделов ИД'
    where code = 'id_priniatie';
update rsk_close_condition set label = 'После выхода приказа ИКС о внесении изменений в РД'
    where code = 'prikaz_iks_rd';
update rsk_close_condition set label = 'После выполнения строй-площадкой работ'
    where code = 'rabota_stroyploshadka';
update rsk_close_condition set label = 'После завершения работ и освобождения складской площадки'
    where code = 'osvobozhdenie_sklad';
update rsk_close_condition set label = 'После получения протоколов уплотнения'
    where code = 'protokoly_uplotneniya';

commit;
