-- Стоп-фактор ИД: справочник вынесен в таблицу (координатор, замечание
-- Болтика В.Н., 07.09.2026: "по возможности пополняемый без правки кода").
-- Раньше — жёстко зашитый в main.py список ID_STOP_FACTORS из 4 значений.
-- Значения и порядок — те же самые, просто перенесены, ничего не меняется
-- для тех, кто уже пользуется формой.

begin;

create table id_stop_factor (
    id            serial primary key,
    description   text not null unique,
    display_order int not null,
    active        boolean not null default true,
    created_at    timestamptz not null default now()
);

insert into id_stop_factor (description, display_order) values
    ('нет паспортов', 1),
    ('нет лаборатории', 2),
    ('нет проектного решения', 3),
    ('работы физически не выполнены', 4);

commit;
