-- Перезаливка справочников ИД с версии Excel от 27.08.2026 (координатор
-- прислал данные текстом в задаче, файл недоступен). Новые данные дают
-- «Ответственные» как настоящий каталог роль→ФИО на уровне ВКЛАДКИ
-- (не колонки вида работ, как было в версии 26.08) — например, на
-- «1. ОПН» 15 пар вроде «Ответственный за формирование АОСР» →
-- «Денисов М.В.». Прежняя модель (responsible_name/signer_name как
-- атрибуты id_form_work_type) для этого не подходит — заводим отдельную
-- таблицу. Колонки id_form_work_type.responsible_name/signer_name не
-- трогаем (могут быть NULL), не чистим по правилу проекта.

begin;

create table id_form_responsible (
    id bigserial primary key,
    tab_id bigint not null references id_form_tab(id),
    role text not null,
    full_name text not null,
    display_order integer not null
);
create index on id_form_responsible(tab_id);

commit;
