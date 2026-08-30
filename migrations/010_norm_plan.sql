-- Плановый график по 56 нормированным позициям сметы ТМ-35 (координатор,
-- 19-20.08.2026). См. docs/smeta_normalization_test_2026-08-19.md.

begin;

create table norm_plan_item (
    id                 bigint generated always as identity primary key,
    smeta_n            integer not null unique,
    name               text not null,
    unit               text,
    qty                numeric,
    matched_source     text,        -- 'СТО-ССР' / 'ГЭСН'
    matched_code       text,
    matched_name       text,
    hours_per_unit     numeric,
    labor_hours_total  numeric,
    assigned_people    integer
);

commit;
