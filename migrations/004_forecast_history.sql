-- Экран "Успеваем?" (реинжиниринг v3, докладная координатора "что делают
-- отраслевые системы", 16.08.2026): тренд-график прогнозной даты по
-- неделям и директивный срок как настраиваемый параметр, не хардкод.
--
-- forecast_snapshot копится оппортунистически: снимок на текущую ISO-неделю
-- записывается при первом открытии /status на этой неделе (не отдельный
-- cron — приложение маленькое, отдельный планировщик избыточен). Если за
-- неделю никто не откроет страницу — снимка за эту неделю не будет; это
-- ограничение отражено в docs, не скрыто.

begin;

create table app_setting (
    key text primary key,
    value text,
    updated_at timestamptz not null default now()
);

create table forecast_snapshot (
    id bigserial primary key,
    snapshot_date date not null,
    iso_year int not null,
    iso_week int not null,
    forecast_date date,
    method text not null,
    remaining_effort_days numeric,
    avg_daily_pace numeric,
    created_at timestamptz not null default now(),
    unique (iso_year, iso_week, method)
);

commit;
