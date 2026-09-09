-- Отчётный слой для экспорта "График ИД" в Excel-вид, привычный части
-- руководства Заказчика (лист "График ИД" книги "01.09.26 График ИД
-- Хабаровск с комм. ред."). В основной модели (id_form_row/id_form_tab)
-- этой группировки нет и не может быть вычислена: раздел там привязан
-- только к вкладке/дисциплине (id_form_tab) — другая ось, без понятия
-- "участок" или "категория конструкции", и без стоимости по КС-2 такого
-- порядка (есть только сумма 10 000-500 000 ₽ в id_folder/id_manual_volume
-- — другая величина для другой цели, не путать).
--
-- Разовые справочные данные заносятся импортом из приложенного
-- Excel-экстракта (docs/import/grafik_id_extract_20260901.csv), не
-- вычисляются программно — координатор, 09.09.2026.
--
-- Отдельная таблица, а не колонки в id_form_row — тот же принцип, что и
-- id_form_responsible (миграция 015): отчётный слой поверх основной
-- модели, не путать с ней, легко откатить целиком, если участковая
-- разметка не приживётся.

begin;

-- type_label, executor_name — сверх присланной координатором схемы:
-- "тип" (фм/рсм) и "Исполнитель" явно перечислены в задаче как колонки
-- экспорта, но источника для них не было бы нигде (id_form_responsible
-- — каталог роль→ФИО НА УРОВНЕ ВКЛАДКИ, миграция 015, это другое понятие,
-- не конкретный исполнитель конкретного раздела), если не занести их тем
-- же самым одноразовым импортом, что участок/категорию/КС2 — не
-- вычисление налету, те же самые поля из того же CSV. См.
-- docs/decisions_needed_grafik_id_export.md.
create table id_row_report_meta (
    id bigserial primary key,
    row_id bigint not null unique references id_form_row(id),
    uchastok_no smallint not null check (uchastok_no between 1 and 4),
    uchastok_label text not null,
    category_group text not null,
    type_label text,
    executor_name text,
    display_order integer not null,
    ks2_cost_mln numeric(12,3),
    source_row integer,
    note text
);
create index on id_row_report_meta(uchastok_no, category_group, display_order);

commit;
