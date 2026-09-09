-- Замена модели вкладки "График ИД" (часть 3, 09.09.2026): строка
-- Excel — почти всегда ГРУППА элементов ("Н1-4" = 5 строк id_form_row),
-- не один элемент. id_row_report_meta (миграция 029, row_id unique)
-- технически не могла принять группу — отсюда 17 из 133 совпадений в
-- части 1, это была ошибка постановки, не брак сопоставления текста.
--
-- id_row_report_meta НЕ удаляется (правило проекта "не чистить"), но с
-- этой задачи экспортом больше не используется — метаданные (участок/
-- категория/тип/исполнитель/КС-2) переехали на уровень группы.

begin;

create table id_report_group (
    id bigserial primary key,
    uchastok_no smallint not null check (uchastok_no between 1 and 4),
    uchastok_label text not null,
    category_group text not null,
    group_label text not null,          -- исходный текст ("Н1-4", "ОПн24, 58, 111.1-114.1, 116")
    type_label text,
    executor_name text,
    display_order integer not null,
    ks2_cost_mln numeric(12,3),
    source_row integer,
    note text
);
create index on id_report_group(uchastok_no, category_group, display_order);

create table id_report_group_row (
    group_id bigint not null references id_report_group(id),
    row_id bigint not null references id_form_row(id),
    primary key (group_id, row_id),
    unique (row_id)   -- один раздел принадлежит не более чем одной группе
);
create index on id_report_group_row(group_id);

comment on table id_row_report_meta is
    'Не используется с 09.09.2026 (часть 3) — заменена id_report_group/'
    'id_report_group_row: строка Excel почти всегда группа из нескольких '
    'id_form_row, эта таблица допускала только один row_id на запись. '
    'Оставлена по правилу проекта "не чистить", не читается экспортом.';

commit;
