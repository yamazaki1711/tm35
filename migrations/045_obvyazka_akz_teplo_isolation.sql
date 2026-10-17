-- Координатор, 17.10.2026: на вкладку «Обвязка» (id_form_tab.code =
-- 'obvyazka', id=10) добавляются два вида работ — «АКЗ» и
-- «Тепло-изоляция». Формулировки — дословно из задания координатора.
-- До миграции на вкладке два вида работ («Монтаж магистральных
-- трубопроводов», «Монтаж трубопроводов», display_order 1–2), новые
-- продолжают нумерацию с 3. Ответственный за ввод данных по вкладке —
-- один (Завгородний А.В.), радиус влияния ограничен его формой
-- /id-entry; колонки responsible_name/signer_name в id_form_work_type —
-- легаси (справочник «Ответственный» читается из id_form_responsible,
-- миграция 015), оставлены NULL.
--
-- Откат: delete from id_form_work_type where id in (select id ...);
-- id фиксируются координатором после накатки (см. run log 17.10.2026).

begin;

insert into id_form_work_type (tab_id, name, display_order)
select 10, v.name, v.ord
from (values ('АКЗ', 3), ('Тепло-изоляция', 4)) as v(name, ord)
where not exists (
    select 1 from id_form_work_type
    where tab_id = 10 and name = v.name
);

commit;
