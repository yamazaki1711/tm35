-- Суточный рапорт как документ (реинжиниринг v3, Цикл 3): погода и подпись
-- ответственного не приходят из Excel вообще — ручные поля на дату,
-- отдельная маленькая таблица, не раздувать daily_progress.

begin;

create table daily_report_meta (
    date        date primary key,
    weather     text,
    signed_by   text,
    updated_at  timestamptz not null default now()
);

commit;
