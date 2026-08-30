-- Координатор, 29.08.2026: «Замечания ИКС и Замечания РСК из ветки СМР
-- удалить полностью как сущность... Деление работ теперь только по
-- двум категориям: основные и вспомогательные».
--
-- Postgres не даёт удалить значение из enum (только добавить), поэтому
-- переносим колонку work.source на новый тип с двумя значениями.
-- Перед этой миграцией ВСЕ строки work уже переведены на source in
-- ('main','aux') (см. отчёт SMR_REBUILD_2026-08-29.md) — миграция
-- только фиксирует это на уровне схемы, данные уже не трогает.

do $$
begin
    if exists (select 1 from work where source not in ('main', 'aux')) then
        raise exception 'work.source содержит значения кроме main/aux — сначала переклассифицировать данные';
    end if;
end $$;

create type work_source_new as enum ('main', 'aux');

alter table work
    alter column source type work_source_new
    using source::text::work_source_new;

drop type work_source;

alter type work_source_new rename to work_source;
