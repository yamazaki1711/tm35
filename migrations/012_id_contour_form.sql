-- Контур ИД — форма ввода по ответам ПТО (TM35_ID_TZ_po_otvetam_PTO.md,
-- 28.08.2026). Единица учёта — РАЗДЕЛ (строка вкладки), не «папка»
-- (терминология поправлена в интерфейсе, таблица id_package не
-- переименована — правило проекта «не чистить»).
--
-- Развилка "раздел vs раздел+вид работ" (см. отчёт, п.2 задания) не
-- решена однозначно данными: реальная структура Excel — матрица
-- (строка-конструкция × вид работ) → статус, то есть атом записи по
-- факту (конструкция, вид работ), а не просто "раздел". Схема ниже
-- сознательно допускает id_form_entry.work_type_id = NULL (для вкладок
-- без деления на виды работ, напр. СОДК), не форсируя единственную
-- трактовку — выбор каскадный на уровне формы.

begin;

-- Справочник вкладок (виды сборки папки/раздела)
create table id_form_tab (
    id bigserial primary key,
    code text not null unique,          -- 'opn', 'opn_rsm', 'truba_svarka', ...
    label text not null,                -- как в Excel: "ОПН", "ОПН (рсм)"
    has_section_level boolean not null default true,  -- есть ли уровень "раздел" отдельно от "конструкция"
    display_order integer not null,
    source_sheet text not null,         -- имя листа в исходном Excel, для трассировки
    has_reference_block boolean not null default false, -- был ли отдельный блок-справочник под таблицей
    note text
);

-- Общий справочник статусов раздела (источник — блок на листе "ОПН",
-- ПТО подтвердил: список идентичен на всех проверенных вкладках).
create table id_form_status (
    id bigserial primary key,
    code text not null unique,          -- короткий код из Excel: 'да', 'нет', 'Перв. пр. 1', ...
    label text not null,                -- полная подпись: "Сформировано в эл.виде"
    display_order integer not null,
    is_stopper boolean not null default false  -- "Нет проектного решения" / "Замечания к площадке" — причины остановки, не стадии конвейера
);

-- Виды работ/этапы — свои у каждой вкладки, с ответственным/подписантом,
-- извлечёнными из шапки таблицы Excel (строки 1/2/4 каждого листа).
create table id_form_work_type (
    id bigserial primary key,
    tab_id bigint not null references id_form_tab(id),
    name text not null,
    display_order integer not null,
    responsible_name text,              -- из строки "Ответственный ФИО" под колонкой
    signer_name text,                   -- из строки "Подписант РСК/АОСР ФИО" над колонкой
    source_col text,                    -- буква колонки Excel, для трассировки
    unique (tab_id, name)
);

-- Строки-разделы/конструкции — атом учёта. section_label может быть
-- NULL там, где в листе нет отдельного уровня "раздел" (Камеры,
-- Траншеи, Колодцы — только "конструкция"/"участок").
create table id_form_row (
    id bigserial primary key,
    tab_id bigint not null references id_form_tab(id),
    section_label text,
    construction_label text not null,
    foundation_label text,
    source_row integer,                 -- номер строки в Excel, для трассировки
    note text
);
create index on id_form_row(tab_id);

-- Ответственные — плоский список уникальных ФИО, встречающихся на
-- вкладке (извлечён из id_form_work_type.responsible_name/signer_name).
-- Отдельной таблицы не заводим — справочник для выпадающего списка
-- формы строится запросом distinct по id_form_work_type текущей
-- вкладки (см. backend), не дублируем данные.

-- Записи формы — КАЖДАЯ отправка отдельная строка (не перезапись).
-- Текущее состояние = последняя запись по (row_id, work_type_id).
create table id_form_entry (
    id bigserial primary key,
    tab_id bigint not null references id_form_tab(id),
    row_id bigint not null references id_form_row(id),
    work_type_id bigint references id_form_work_type(id),  -- NULL допустим (вкладки без деления на виды работ)
    responsible_name text not null,
    status_id bigint not null references id_form_status(id),
    status_date date not null,
    planned_rsk_date date,
    blocker_id bigint references blocker(id),  -- стоп-фактор — существующий механизм, не новый
    comment text,
    created_by bigint references app_user(id),
    created_at timestamptz not null default now()
);
create index on id_form_entry(row_id, work_type_id, created_at desc);
create index on id_form_entry(tab_id);

-- Блокировка ИЗМ — признак, НЕ статус (ПТО: "статус при блокировке
-- сохраняется, после снятия работа продолжается с того же места").
-- Отдельная сущность с датой установки/снятия, привязана к тому же
-- атому (row_id, work_type_id), что и записи статуса.
create table id_form_block (
    id bigserial primary key,
    row_id bigint not null references id_form_row(id),
    work_type_id bigint references id_form_work_type(id),
    change_ref text,                    -- номер/название ИЗМ, свободный текст (change пуста, не FK)
    blocked_at date not null,
    unblocked_at date,
    comment text,
    created_by bigint references app_user(id),
    created_at timestamptz not null default now()
);
create index on id_form_block(row_id, work_type_id);

commit;
