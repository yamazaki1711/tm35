-- ТМ-35 Мониторинг — начальная схема (MVP)
-- Источник: ТЗ_ТМ-35.odt раздел 12 (модель данных) + docs/РЕШЕНИЯ_v1.1.md
-- (переименование fact_daily -> daily_progress, добавление daily_progress.source,
-- таблицы-заготовки под симуляцию без логики).
-- БД: tm35, отдельная от bi.asd-kontur.ru (другой ОКС) — см. CLAUDE.md.

begin;

-- ---------------------------------------------------------------------
-- Справочники (закрытые словари — ТЗ п. 8.2, 8.7, 8.8)
-- ---------------------------------------------------------------------

create type work_source as enum ('main', 'iks', 'rsk', 'aux');
-- main = основной график ТМ-35 (TM35-MAIN-###)
-- iks  = замечания ИКС (TM35-IKS-###)
-- rsk  = замечания РСК (TM35-RSK-###)
-- aux  = вспомогательные работы (TM35-AUX-###)

create type work_status as enum (
    'not_started',      -- Не начата
    'in_progress',       -- В работе
    'suspended',         -- Приостановлена
    'limited',           -- Ограничена
    'done_physically',   -- Выполнена физически
    'submitted',         -- Предъявлена
    'accepted',          -- Принята
    'closed',            -- Закрыта
    'cancelled'           -- Отменена / перенесена
);

create type executor_type as enum ('own_forces', 'subcontract');

create type blocker_type as enum (
    'material', 'delivery', 'equipment', 'fuel', 'weather', 'front',
    'design_decision', 'subcontract', 'contract', 'payment',
    'acceptance', 'sequence', 'aux_reallocation'
);

create type blocker_status as enum ('active', 'resolved');

create type reason_code as enum (
    'WEATHER_RAIN', 'WEATHER_WIND', 'WEATHER_TEMP',
    'FUEL_MISSING',
    'MATERIAL_MISSING', 'MATERIAL_DELIVERY',
    'EQUIPMENT_MISSING', 'EQUIPMENT_BROKEN',
    'FRONT_MISSING', 'DESIGN_MISSING',
    'SUBCONTRACT_MISSING', 'CONTRACT_NOT_SIGNED', 'PAYMENT_MISSING',
    'AUX_REALLOCATION', 'ACCEPTANCE_WAIT', 'SEQUENCE_WAIT',
    'PLANNING_ERROR', 'OTHER'
);

create type daily_progress_source as enum ('excel_import', 'web_form');

create type material_status as enum (
    'requested', 'ordered', 'paid', 'in_transit', 'on_site', 'missing'
);

create type app_role as enum (
    'admin', 'planner', 'executor', 'customer', 'manager'
);

-- Явный статус качества данных на уровне записи — заполняется при импорте,
-- когда парсер не может однозначно интерпретировать исходную ячейку
-- (см. import/parse_excel.py и .claude/skills/tm35-excel/SKILL.md).
-- 'ok' — распознано без замечаний; 'needs_review' — загружено, но требует
-- сверки человеком (значение не додумывается автоматически).
create type data_quality_flag as enum ('ok', 'needs_review');

-- ---------------------------------------------------------------------
-- Пользователи (минимально необходимая модель для ролей из ТЗ 6.1)
-- ---------------------------------------------------------------------

create table app_user (
    id           bigint generated always as identity primary key,
    full_name    text not null,
    email        text unique,
    role         app_role not null,
    is_active    boolean not null default true,
    created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 12.1.1 work — справочник работ
-- ---------------------------------------------------------------------

create table work (
    id               bigint generated always as identity primary key,
    code             text not null unique,           -- TM35-MAIN-001 и т.п.
    source           work_source not null,
    location         text,                            -- участок/локация (УТ, КР, КМ, ...)
    name             text not null,
    unit             text,                            -- ед. изм.
    volume           numeric,                         -- плановый объём
    weight           numeric,                         -- вес для расчёта прогресса
    work_type        text,
    executor_type    executor_type not null default 'own_forces',
    responsible_id   bigint references app_user(id),
    subcontractor_id bigint,                          -- fk добавлен ниже после создания subcontractor
    status           work_status not null default 'not_started',
    criticality      smallint,                        -- 1..5, приоритет/критичность
    comment          text,
    section          text,                             -- подраздел листа-источника ("Работы по устранению недостатков" и т.п.) — для трассировки к исходным секциям Excel, не заменяет work_source
    source_row_ref   text,                             -- ссылка на исходную ячейку Excel (лист!строка) для трассировки при миграции
    fact_pct         numeric,                          -- нормализованный "% вып." из шапки работы (не путать с daily_progress.fact_pct — это снимок на дату выгрузки Excel)
    fact_pct_raw     text,                             -- исходное значение "% вып." до нормализации (может быть "16шт" и т.п. — ТЗ 11.4.2)
    data_quality_flag data_quality_flag not null default 'ok',
    data_quality_note text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create index idx_work_source on work(source);
create index idx_work_status on work(status);
create index idx_work_data_quality on work(data_quality_flag) where data_quality_flag <> 'ok';

-- ---------------------------------------------------------------------
-- 12.1.2 baseline_schedule — базовый график
-- ---------------------------------------------------------------------

create table baseline_schedule (
    id            bigint generated always as identity primary key,
    work_id       bigint not null references work(id),
    plan_start    date,
    plan_finish   date,
    plan_crew     integer,
    plan_man_days numeric,
    approved_by   bigint references app_user(id),
    approved_at   timestamptz,
    comment       text
);

create index idx_baseline_schedule_work on baseline_schedule(work_id);

-- ---------------------------------------------------------------------
-- 12.1.3 current_schedule — текущий актуальный график
-- ---------------------------------------------------------------------

create table current_schedule (
    id              bigint generated always as identity primary key,
    work_id         bigint not null references work(id),
    current_start   date,
    current_finish  date,
    forecast_finish date,
    planned_crew    integer,
    planned_man_days numeric,
    updated_by      bigint references app_user(id),
    updated_at      timestamptz not null default now(),
    reason          text
);

create index idx_current_schedule_work on current_schedule(work_id);

-- ---------------------------------------------------------------------
-- 12.1.4 daily_progress (в ТЗ v0.1 — fact_daily) — ежедневный факт.
-- РЕШЕНИЯ_v1.1: два канала ввода в одну таблицу, различаются полем source.
-- Конфликт (Excel и веб-форма за одну дату/работу) разрешается по
-- последней по времени записи (updated_at) — обе версии остаются в
-- audit_log, эта таблица хранит только текущее состояние.
-- ---------------------------------------------------------------------

create table daily_progress (
    id              bigint generated always as identity primary key,
    date            date not null,
    work_id         bigint not null references work(id),
    planned_crew    integer,
    actual_crew     integer,
    planned_hours   numeric,
    actual_hours    numeric,
    stop_hours      numeric,
    done_volume     numeric,
    fact_pct        numeric,               -- нормализованный процент (0..100)
    fact_pct_raw    text,                  -- исходное значение из Excel ("16шт", "9 шт" и т.п. — ТЗ 11.4.2)
    status          work_status,
    reason_code     reason_code,
    comment         text,
    source          daily_progress_source not null,
    data_quality_flag data_quality_flag not null default 'ok',
    data_quality_note text,
    created_by      bigint references app_user(id),  -- null для excel_import
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (date, work_id, source)
);

create index idx_daily_progress_date on daily_progress(date);
create index idx_daily_progress_work on daily_progress(work_id);
create index idx_daily_progress_data_quality on daily_progress(data_quality_flag) where data_quality_flag <> 'ok';

-- ---------------------------------------------------------------------
-- 12.1.5 resource_reallocation — переброска ресурса
-- ---------------------------------------------------------------------

create table resource_reallocation (
    id           bigint generated always as identity primary key,
    date         date not null,
    from_work_id bigint references work(id),
    to_work_id   bigint references work(id),
    people_count integer not null,
    reason_code  reason_code,
    comment      text,
    created_by   bigint references app_user(id),
    created_at   timestamptz not null default now()
);

create index idx_resource_reallocation_date on resource_reallocation(date);

-- ---------------------------------------------------------------------
-- 12.1.6 blocker — реестр ограничений
-- ---------------------------------------------------------------------

create table blocker (
    id                      bigint generated always as identity primary key,
    work_id                 bigint references work(id),  -- null = относится к группе работ/участку в целом
    blocker_type            blocker_type not null,
    description             text not null,
    status                  blocker_status not null default 'active',
    owner_id                bigint references app_user(id),
    created_at              timestamptz not null default now(),
    expected_resolution_date date,
    actual_resolution_date  date,
    impact_days             integer,
    comment                 text
);

create index idx_blocker_work on blocker(work_id);
create index idx_blocker_status on blocker(status);

-- ---------------------------------------------------------------------
-- 12.1.7 resource_pool — ресурсный пул по датам
-- ---------------------------------------------------------------------

create table resource_pool (
    id             bigint generated always as identity primary key,
    date           date not null unique,
    available_total integer,
    actual_total    integer,
    assigned_main   integer,
    assigned_aux    integer,
    assigned_iks    integer,
    assigned_rsk    integer,
    idle            integer,
    deficit         integer,
    comment         text
);

-- ---------------------------------------------------------------------
-- 12.1.8 subcontractor — субподрядчики
-- ---------------------------------------------------------------------

create table subcontractor (
    id                   bigint generated always as identity primary key,
    name                 text not null,
    work_type            text,
    contract_status      text,               -- напр. "на стадии заключения", "подписан"
    mobilization_status  text,
    expected_start_date  date,
    actual_start_date    date,
    crew_size            integer,
    reason_delayed       text,
    impact               text,
    comment              text,
    coordinator_id       bigint references app_user(id)
);

alter table work
    add constraint fk_work_subcontractor
    foreign key (subcontractor_id) references subcontractor(id);

-- ---------------------------------------------------------------------
-- 12.1.9 material — материалы и поставки
-- 12.1.10 material_work_link
-- ---------------------------------------------------------------------

create table material (
    id                     bigint generated always as identity primary key,
    name                   text not null,
    supplier               text,
    status                 material_status not null default 'requested',
    expected_delivery_date date,
    actual_delivery_date   date,
    blocks_work            boolean not null default false,
    comment                text
);

create table material_work_link (
    material_id bigint not null references material(id),
    work_id     bigint not null references work(id),
    required    boolean not null default true,
    comment     text,
    primary key (material_id, work_id)
);

-- ---------------------------------------------------------------------
-- 12.1.11 equipment — техника
-- 12.1.12 equipment_work_link
-- ---------------------------------------------------------------------

create table equipment (
    id          bigint generated always as identity primary key,
    name        text not null,
    status      text,       -- доступна / поломка / занята на другом объекте / нет оператора
    fuel_status text,
    comment     text
);

create table equipment_work_link (
    equipment_id bigint not null references equipment(id),
    work_id      bigint not null references work(id),
    required     boolean not null default true,
    comment      text,
    primary key (equipment_id, work_id)
);

-- ---------------------------------------------------------------------
-- 12.1.13 scenario / 12.1.14 scenario_result — ЗАГОТОВКА под симуляцию.
-- РЕШЕНИЯ_v1.1: модуль симуляции отложен во 2-ю итерацию. Таблицы создаются
-- сейчас, чтобы миграция под них не потребовала болезненного ALTER, но
-- никакой расчётной логики/API/UI поверх них в MVP нет.
-- ---------------------------------------------------------------------

create table scenario (
    id                 bigint generated always as identity primary key,
    name               text not null,
    target_date        date,
    scope              text,
    available_people   integer,
    shift_hours        numeric,
    utilization_coeff  numeric,
    sub1_start_date    date,
    sub2_start_date    date,
    overtime_enabled   boolean not null default false,
    created_by         bigint references app_user(id),
    created_at         timestamptz not null default now()
);

create table scenario_result (
    id                  bigint generated always as identity primary key,
    scenario_id         bigint not null references scenario(id),
    required_people     integer,
    deficit_people      integer,
    forecast_finish     date,
    achievable          boolean,
    critical_works      jsonb,
    blockers            jsonb,
    calculation_payload jsonb,
    created_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 12.1.15 decision_log — журнал решений и уведомлений
-- ---------------------------------------------------------------------

create table decision_log (
    id                 bigint generated always as identity primary key,
    date               date not null default current_date,
    decision_type      text,
    decision_text      text not null,
    responsible_party  text,
    document_reference text,
    comment            text,
    created_at         timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 12.1.16 audit_log — журнал изменений
-- ---------------------------------------------------------------------

create table audit_log (
    id          bigint generated always as identity primary key,
    user_id     bigint references app_user(id),
    entity_type text not null,
    entity_id   bigint,
    action      text not null,      -- insert/update/delete/import
    old_value   jsonb,
    new_value   jsonb,
    reason      text,
    created_at  timestamptz not null default now()
);

create index idx_audit_log_entity on audit_log(entity_type, entity_id);

-- ---------------------------------------------------------------------
-- import_unresolved_cell — ячейки исходного Excel, которые парсер не смог
-- превратить в корректную запись daily_progress (например, календарная
-- дата не определилась однозначно — нет валидного `date` для NOT NULL
-- колонки, вставить строку с угаданной датой нельзя). Не заменяет
-- data_quality_flag на daily_progress/work — это отдельные случаи, для
-- которых валидной строки в целевой таблице ещё не существует.
-- ---------------------------------------------------------------------

create table import_unresolved_cell (
    id           bigint generated always as identity primary key,
    sheet        text not null,
    cell_ref     text not null,           -- напр. "JH26"
    work_code    text,                    -- код работы, если строка распознана, иначе null
    issue_type   text not null,           -- напр. "bad_calendar_date"
    raw_payload  jsonb not null,          -- то, что успел собрать парсер (см. quality_report.json)
    resolved     boolean not null default false,
    resolution_note text,
    created_at   timestamptz not null default now()
);

create index idx_import_unresolved_cell_open on import_unresolved_cell(resolved) where not resolved;

commit;
