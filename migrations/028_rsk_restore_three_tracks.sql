-- Откат миграции 025 (координатор, 07.09.2026): схлопывание трёх треков
-- РСК (Физика/Проект/ИД) в одну пару категория+статус не согласовывалось —
-- это была самодеятельность сессии 06.09.2026. Треки — три независимых
-- поля с разной семантикой, не один статус:
--   track_phys   — устранено ли фактически на площадке (у него одного
--                  есть "факт" — выполнено с отступлением от проекта,
--                  нужна корректировка РД, задача для ДПР, не "выполнено");
--   track_design — требуется ли корректировка ПД/РД и сделана ли;
--   track_id     — представлена ли исполнительная документация.
--
-- rsk_processing была пуста и на момент введения категории+статуса (см.
-- 025), и сейчас (проверено: select count(*) = 0) — восстановление полей
-- без потери данных, backfill не требуется.

begin;

alter table rsk_processing drop constraint if exists rsk_processing_fact_only_phys;
alter table rsk_processing drop column if exists category;
alter table rsk_processing drop column if exists status;

alter table rsk_processing
    add column track_phys text not null default 'unknown'
        check (track_phys in ('not_required', 'not_done', 'done', 'fact', 'unknown')),
    add column track_design text not null default 'unknown'
        check (track_design in ('not_required', 'not_done', 'done', 'unknown')),
    add column track_id text not null default 'unknown'
        check (track_id in ('not_required', 'not_done', 'done', 'unknown'));

commit;
