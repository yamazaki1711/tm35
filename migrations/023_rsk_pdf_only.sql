-- Контур РСК — пересмотр (координатор, 06.09.2026, повторно тем же
-- вечером после 022_rsk_contour.sql): первоисточник — ТОЛЬКО PDF-акт
-- проверки, не Excel-реестр. "Excel-файл, который сейчас ведёт инженер
-- ПТО, — это отдельный слой «как замечание отрабатывается внутри»; он
-- подключается следующим этапом и текстами нарушений не является."
--
-- 022_rsk_contour.sql строила схему и импорт от Excel-реестра (state
-- по текстовым эвристикам "снято"/"отклонено" в комментариях, треки
-- Физика/Проект/ИД на самой rsk_violation, ответственные и условия
-- снятия как m2m/справочники). Координатор явно отменил этот подход —
-- задача на этом этапе уже: "только извлечение и загрузка" из PDF,
-- ничего сверх этого (треки/ответственные/аналитика — отдельно,
-- следующим этапом, другим промптом). Схема ниже проще: снятие
-- определяется по ДИФФУ между актами (при появлении второго акта), не
-- по тексту; текст нарушения живёт в позиции акта (rsk_act_item), не
-- дублируется в rsk_violation — от акта к акту формулировка может
-- меняться.
--
-- Старые таблицы удаляются целиком, вместе с данными разбора реестра —
-- они были только разовым срезом для сверки (см. docs/
-- RSK_KONTUR_IMPORT_2026-09-06.md), не production-данными, которыми
-- кто-то уже пользуется. /rsk (read-only страница меню) и пункт меню
-- «РСК» тоже убраны в этом же коммите — координатор явно попросил
-- "UI не делать" на этом этапе, представление будет отдельной задачей.

begin;

drop table if exists rsk_act_item cascade;
drop table if exists rsk_violation_responsible cascade;
drop table if exists rsk_violation cascade;
drop table if exists rsk_act cascade;
drop table if exists rsk_close_condition cascade;
drop table if exists rsk_control_measure cascade;
drop table if exists rsk_responsible cascade;
drop table if exists rsk_import_issue cascade;

-- Акт проверки РСК.
create table rsk_act (
    id bigserial primary key,
    act_no text not null unique,        -- '4183-159'
    act_date date not null,
    total_declared integer,             -- «Общее количество нарушений» из текста акта
    pdf_path text,
    imported_ts timestamptz not null default now()
);

-- Нарушение — сквозная сущность по системному номеру РСК. Канонического
-- текста здесь нет намеренно: актуальная формулировка — это позиция
-- (rsk_act_item) из последнего акта, где нарушение встречается.
create table rsk_violation (
    id bigserial primary key,
    sys_no integer not null unique,
    first_act_no text,
    first_detected_date date
);

-- Вхождение нарушения в конкретный акт — своя формулировка текста в
-- каждом акте, своя дата срока устранения. "Снято" узнаём диффом (акт N
-- есть, акт N+1 этого sys_no не содержит) — механики диффа/полей под
-- неё в этой миграции сознательно нет: акт пока один, вычислять нечего,
-- проектируется вместе со вторым актом, чтобы не гадать вслепую.
create table rsk_act_item (
    id bigserial primary key,
    act_id bigint not null references rsk_act(id),
    violation_id bigint not null references rsk_violation(id),
    item_no text,                       -- '2.14' — № п/п из акта (раздел.позиция)
    control_section smallint,           -- 1..4, целая часть item_no
    content text not null,
    remedy text,
    due_date date,
    is_repeat bool,
    unique (act_id, violation_id)
);
create index on rsk_act_item(violation_id);

commit;
