-- Контур РСК — предписания Федерального центра строительного контроля
-- (ФБУ «РосСтройКонтроль») по объекту ТМ-35. Этап 1 (докс координатора
-- "РСК контур в АСД — этап 1", 06.09.2026): только модель данных и
-- разовый импорт из реестра Excel + акта проверки PDF, UI не делается —
-- проектируется отдельно после проверки качества загруженных данных
-- (см. import/rsk/README.md и rsk_import_issue после загрузки).

begin;

-- Контрольное мероприятие — колонка "Контрольное мероприятие" реестра,
-- всего 4 варианта во всём источнике (пронумерованы по ст. 53 ГрК РФ,
-- цифра в начале текста — из самого источника, не наша нумерация).
create table rsk_control_measure (
    id bigserial primary key,
    code text not null unique,          -- короткий код для трассировки: 'vhodnoy', 'posledovatelnost', 'skladirovanie', 'inoe'
    label text not null                 -- полный текст из колонки B реестра
);

-- Ответственные — нормализованный справочник (в источнике 22 варианта
-- написания на эти 5 сущностей, см. import/rsk/parse_rsk.py::RESP_CANON).
create table rsk_responsible (
    id bigserial primary key,
    name text not null unique
);

-- Условия снятия — типовые формулировки колонки "Плановая дата снятия
-- замечнаий" (опечатка в самом источнике), когда там не дата и не текст
-- отклонения/заметки, а условие-триггер ("Неделя после ..."). Близкие
-- формулировки схлопнуты в одну запись при импорте, исходный текст (если
-- отличается содержательно — напр. требуется ещё и подписанный АОСР) —
-- в rsk_violation.note, не здесь.
create table rsk_close_condition (
    id bigserial primary key,
    code text not null unique,
    label text not null
);

-- Акт проверки РСК. total_violations/pdf_path заполнены только для
-- акта, PDF которого реально был на этом этапе (4183-159) — для актов,
-- известных только по колонкам "Включён впервые/в последний раз"
-- реестра, оба поля NULL (докс, п. rsk_act_item — "полную историю
-- вхождений построим, когда накопятся акты").
create table rsk_act (
    id bigserial primary key,
    act_no text not null unique,        -- '4183-159'
    act_date date not null,
    total_violations integer,
    pdf_path text
);

-- Нарушение — единица учёта контура РСК.
create table rsk_violation (
    id bigserial primary key,
    sys_no integer not null unique,     -- системный № РСК (сквозной, из реестра/акта)
    control_measure_id bigint references rsk_control_measure(id),
    content text not null,
    remedy text,
    violation_type text not null default 'unknown'
        check (violation_type in ('significant', 'critical', 'unknown')),
    section_raw text,                   -- участок, извлечён регуляркой из content — без связи с объектами СМР (этап 1)
    is_repeat bool not null default false,  -- пометка "Повторно" в акте 4183-159 (единственный акт с полным текстом на этом этапе)
    created_at date,                    -- дата создания замечания РСК; NULL, если не удалось распарсить свободный текст колонки "Создано"
    due_date date,
    due_date_moved date,
    urgent bool not null default false,
    author text,
    state text not null
        check (state in ('open', 'submitted', 'rejected', 'partially_closed', 'closed')),
    closed_date date,
    close_condition_id bigint references rsk_close_condition(id),
    note text,
    source text not null check (source in ('registry', 'act')),
    created_ts timestamptz not null default now(),
    updated_ts timestamptz not null default now(),

    -- Три независимых трека — разная семантика, отдельные енумы (докс:
    -- "единый справочник на все три не натягивать"; 'fact' — только в
    -- track_phys).
    track_phys text not null default 'unknown'
        check (track_phys in ('not_required', 'not_done', 'done', 'fact', 'other', 'unknown')),
    track_phys_raw text,
    track_design text not null default 'unknown'
        check (track_design in ('not_required', 'not_done', 'done', 'other', 'unknown')),
    track_design_raw text,
    track_id text not null default 'unknown'
        check (track_id in ('not_required', 'not_done', 'done', 'other', 'unknown')),
    track_id_raw text
);
create index on rsk_violation(state);
create index on rsk_violation(close_condition_id);

-- Ответственные за нарушение — m2m (в источнике одна ячейка может нести
-- несколько сущностей через "+": "ДПР+Стройка ТМ-35").
create table rsk_violation_responsible (
    violation_id bigint not null references rsk_violation(id),
    responsible_id bigint not null references rsk_responsible(id),
    primary key (violation_id, responsible_id)
);

-- Вхождение нарушения в акт — m2m. item_no/due_date/is_repeat известны
-- только для акта, чей PDF реально разобран (4183-159); для актов,
-- известных лишь по колонкам реестра "Включён впервые/в последний раз",
-- эти три поля NULL — история по ним не восстановима без самих PDF.
create table rsk_act_item (
    id bigserial primary key,
    act_id bigint not null references rsk_act(id),
    violation_id bigint not null references rsk_violation(id),
    item_no text,
    due_date date,
    is_repeat bool,
    unique (act_id, violation_id)
);

-- Отчёт сверки импорта — не «чинить» дефекты источника молча, каждое
-- отклонение от карты нормализации/ожидаемой структуры — отдельная
-- строка на ручной разбор.
create table rsk_import_issue (
    id bigserial primary key,
    sys_no integer,
    severity text not null check (severity in ('error', 'warning', 'info')),
    kind text not null,
    message text not null,
    raw_value text,
    created_ts timestamptz not null default now()
);

commit;
