-- Задача 3 (СМР), ТЗ Якименко А.И. 16.09.2026 — перед правкой "график не
-- обновляется" (§3) обнаружено: таблица `current_schedule` (сдвиг сроков
-- через /gantt, `/api/gantt/schedule`) и столбец `work.plan_finish_date`
-- (писал старый /form) существуют и работают в проде, но НИ ОДНОЙ
-- миграции, которая бы их создавала, в git нет. Не единственный такой
-- случай в проекте (`id_folder`, `change`, `prescription`, `id_package`
-- и другие тоже не заведены ни одной миграцией, как и не используемая
-- нигде в коде `simple_form_hidden` — старый, уже принятый разрыв, вне
-- периметра этой задачи, см. run log и KNOWN_ISSUES.md). Здесь заводится
-- только то, что напрямую нужно для правки этой задачи — `if not exists`,
-- идемпотентно: на живой базе, где всё это уже есть, миграция не меняет
-- ничего, а на чистой базе, поднятой строго из migrations/, воспроизводит
-- рабочую схему.

begin;

create table if not exists current_schedule (
    id               bigint generated always as identity primary key,
    work_id          bigint not null references work(id),
    current_start    date,
    current_finish   date,
    forecast_finish  date,
    planned_crew     integer,
    planned_man_days numeric,
    updated_by       bigint references app_user(id),
    updated_at       timestamptz not null default now(),
    reason           text
);
create index if not exists idx_current_schedule_work on current_schedule(work_id);

alter table work add column if not exists plan_finish_date date;
create index if not exists idx_work_plan_finish on work(plan_finish_date) where plan_finish_date is not null;

commit;
