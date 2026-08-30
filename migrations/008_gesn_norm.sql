-- Справочник норм ГЭСН-2022 (12 сборников, докачано и проверено вручную
-- 19.08.2026 — docs/spravochnik_GESN_TM35.xlsx/.md). Самостоятельный
-- каталог, НЕ привязан к конкретным работам ПТО (163 работы Excel не
-- сопоставляются с ним автоматически — единица измерения у большинства
-- «комп.», не физический объём; см. CLAUDE.md, раздел «Справочник норм
-- трудозатрат», пп. 3-4 «Истории замен»).

begin;

create table gesn_norm (
    id             bigint generated always as identity primary key,
    sbornik_code   text not null,     -- 'GESN_24_Teplosnabzhenie' и т.п.
    sbornik_title  text not null,     -- 'ГЭСН 81-02-24-2022 Сборник 24. ...'
    code           text not null,     -- '24-01-001-01'
    name           text not null,
    unit           text,
    hours_per_unit numeric,           -- null = не извлечено автоматически (честно, не выдумано)
    unique (sbornik_code, code)
);

create index idx_gesn_norm_code on gesn_norm(code);
create index idx_gesn_norm_sbornik on gesn_norm(sbornik_code);

-- Задел под будущее ручное присвоение нормы конкретной работе (UI пока
-- не строится — нет надёжного способа получить реальный физический
-- объём по большинству работ ПТО, см. комментарий выше). Колонка нужна
-- сразу, чтобы не делать вторую миграцию, когда способ ввода объёма
-- будет согласован с координатором.
alter table work add column gesn_norm_id bigint references gesn_norm(id);

commit;
