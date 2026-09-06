-- РСК: три независимых трека (Физика/Проект/ИД) заменены одной категорией
-- + одним статусом выполнения (координатор, 07.09.2026: "структура таблицы
-- в Реестр предписаний РСК неправильная... правильно оформить одну колонку
-- категория"). Модель была: у нарушения три параллельных статуса, у
-- каждого свой набор значений (не треб./не вып./вып./факт). Стало: у
-- нарушения ровно ОДНА категория (к какому треку оно относится) и ОДИН
-- статус выполнения по этой категории — "не треб." как значение больше не
-- нужно (третья категория просто не выбрана, а не помечена "не треб.").
-- "факт" (работы выполнены с отступлением от проекта, нужна корректировка
-- РД) остаётся смыслово привязан только к категории "phys" — проверяется
-- check-constraint, не только на уровне формы.
--
-- rsk_processing на момент правки пуст (проверено: select count(*) = 0,
-- форма ещё не использовалась вживую) — безопасно дропать колонки без
-- переноса данных.

begin;

alter table rsk_processing
    add column category text check (category in ('phys', 'design', 'id')),
    add column status text check (status in ('not_done', 'done', 'fact'));

alter table rsk_processing
    add constraint rsk_processing_fact_only_phys
    check (status is distinct from 'fact' or category = 'phys');

alter table rsk_processing drop column track_phys;
alter table rsk_processing drop column track_design;
alter table rsk_processing drop column track_id;

commit;
