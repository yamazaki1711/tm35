-- Справочник норм СТО-ССР-2026 (Spider Project) — основной справочник
-- для ТМ-35 по решению координатора (19.08.2026), gesn_norm остаётся
-- вспомогательным. См. CLAUDE.md, раздел «Второй справочник — СТО-ССР».

begin;

-- code НЕ уникален — в источнике (СТО-ССР, версия 4) один и тот же код
-- изредка используется для разных операций (напр. "(ЗР-СреГру)" — и
-- бульдозером, и экскаватором; "(НО/УП)" — служебная заглушка
-- "не определено/уточняется" для 7 разных операций Устройства камеры).
-- Проверено по исходнику — не ошибка разбора. Первичный ключ — id.
create table ssr_norm (
    id                          bigint generated always as identity primary key,
    section                     text not null,          -- 'Устройство трубопроводов (УТ)'
    code                        text not null,           -- 'УТ-СваТру110'
    name                        text not null,
    unit                        text,
    team_productivity_per_hour  numeric,
    labor_hours_per_unit        numeric,                 -- null = нет людской составляющей (не выдумано)
    crew                        jsonb,                   -- [{resource, unit, qty, loading_pct, resource_productivity}, ...]
    notes                       text
);

create index idx_ssr_norm_section on ssr_norm(section);

-- Задел под будущее ручное присвоение нормы работе (та же логика, что
-- у gesn_norm_id — UI пока не строится, нет реальных объёмов у ПТО).
alter table work add column ssr_norm_id bigint references ssr_norm(id);

commit;
