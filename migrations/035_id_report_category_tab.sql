-- Заход 4, 10.09.2026, задача 1. Решение "категория группы Графика ИД
-- -> какие вкладки к ней относятся" — координатор ставит галочки,
-- ничего не предзаполняется автоматически. Многие-ко-многим: одна
-- категория обычно относится сразу к нескольким вкладкам ("Камеры и
-- колодцы" -> Камеры + Колодцы).
begin;

create table id_report_category_tab (
    id bigserial primary key,
    category_group text not null,
    tab_id bigint not null references id_form_tab(id),
    created_by bigint,
    created_at timestamptz not null default now(),
    unique (category_group, tab_id)
);

commit;
