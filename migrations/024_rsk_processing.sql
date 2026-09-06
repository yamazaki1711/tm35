-- Контур РСК — слой 2 (отработка) + каркас ветки меню, координатор 06.09.2026
-- ("Ветка РСК: архитектура раздела"). Два слоя данных, не смешивать:
--   слой 1 (акт, неизменяемый) — rsk_act/rsk_act_item/rsk_violation, уже есть;
--   слой 2 (отработка, изменяемый людьми) — rsk_processing ниже, 1:1 с
--   rsk_violation. Повторная загрузка акта (слой 1) НЕ трогает rsk_processing —
--   гарантия, что работа ПТО не затирается очередным актом.
--
-- Снятие нарушения — не текстовая эвристика, а факт: отсутствие в
-- последнем загруженном акте (диф при импорте). Хранится на rsk_violation
-- (is_active/closed_in_act_id), проставляется импортом, вручную не
-- редактируется — тот же принцип, что и весь слой 1.

begin;

alter table rsk_violation add column is_active boolean not null default true;
alter table rsk_violation add column closed_in_act_id bigint references rsk_act(id);

-- Ответственные — тот же справочник, что уже был в отменённой схеме 022
-- (Excel-реестр подтвердил ровно эти 5 сущностей), пересоздаётся заново,
-- т.к. 023 удалила его вместе со всей той схемой.
create table rsk_responsible (
    id bigserial primary key,
    name text not null unique
);
insert into rsk_responsible (name) values
    ('ПТО'), ('ДПР'), ('Стройка ТМ-35'), ('ИКС'), ('Лаборатория');

-- Условия снятия — справочник, не свободный текст. Список не исчерпывающий
-- (докс явно говорит "и прочие") — отделка следующим этапом, добавлять по
-- мере появления новых формулировок в реальной отработке.
create table rsk_close_condition (
    id bigserial primary key,
    code text not null unique,
    label text not null
);
insert into rsk_close_condition (code, label) values
    ('id_priniatie', 'Неделя после принятия РСК соответствующих разделов ИД'),
    ('prikaz_iks_rd', 'Неделя после выхода приказа ИКС о внесении изменений в РД'),
    ('rabota_stroyploshadka', 'Неделя после выполнения Стр.площадкой работ'),
    ('osvobozhdenie_sklad', 'Неделя после завершения работ и освобождения складской площадки'),
    ('protokoly_uplotneniya', 'Неделя после получения протоколов уплотнения');

-- Слой 2 — отработка предписания. 1:1 с rsk_violation (по violation_id,
-- не по sys_no напрямую — тот же ключ, что везде в слое 1). "Факт" у
-- track_phys — не "выполнено", а "выполнено с отступлением от проекта,
-- нужна корректировка РД" (докс, форма 2) — задача для ДПР, поэтому
-- отдельное значение, не синоним done.
--
-- "Отклонено РСК" (нужно для плитки "Обзор РСК") — источника для этого в
-- слое 1 нет (в отличие от отменённой Excel-схемы, где это выводилось из
-- текста комментария). Заведено явным полем `rejected`, проставляется
-- человеком в форме 2 — простейший вариант каркаса, точная семантика
-- (когда именно это "отклонено", кем) — отделка следующим этапом.
create table rsk_processing (
    id bigserial primary key,
    violation_id bigint not null unique references rsk_violation(id),
    track_phys text not null default 'unknown'
        check (track_phys in ('not_required', 'not_done', 'done', 'fact', 'unknown')),
    track_design text not null default 'unknown'
        check (track_design in ('not_required', 'not_done', 'done', 'unknown')),
    track_id text not null default 'unknown'
        check (track_id in ('not_required', 'not_done', 'done', 'unknown')),
    close_condition_id bigint references rsk_close_condition(id),
    planned_close_date date,
    rejected boolean not null default false,
    comment text,
    created_ts timestamptz not null default now(),
    updated_ts timestamptz not null default now()
);

create table rsk_processing_responsible (
    processing_id bigint not null references rsk_processing(id),
    responsible_id bigint not null references rsk_responsible(id),
    primary key (processing_id, responsible_id)
);

-- Связь с ИД, м2м — ради неё оба контура и в одной системе (докс).
-- В карточке предписания видно разделы ИД, которые его закроют; из
-- раздела ИД видно предписания, которые снимутся после его принятия.
create table rsk_processing_id_row (
    processing_id bigint not null references rsk_processing(id),
    id_form_row_id bigint not null references id_form_row(id),
    primary key (processing_id, id_form_row_id)
);
create index on rsk_processing_id_row(id_form_row_id);

commit;
