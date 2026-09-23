-- ТЗ Якименко А.И. № 10 (23.09.2026), пункт 5 — новый сектор «КС-3» в
-- самом низу /id-folders, зеркалирует «Невыбираемый остаток»
-- (id_manual_volume): описание + сумма, add/edit/delete, те же права
-- (id-folders:submit), тот же учёт автора/даты. В отличие от
-- id_manual_volume (только create/delete) сектор КС-3 по заданию должен
-- поддерживать ещё и правку — добавлены updated_at/updated_by, чтобы
-- редактирование отслеживалось так же, как создание, а не терялось
-- молча (тот же принцип, что rsk_processing/id_folder).
-- Сумма тайла «Подписано по КС-3» — простая sum(amount_rub) по всем
-- строкам, без дискриминатора типа: заданию не нужно различать виды
-- записей внутри сектора, только сложить их (см. compute_id_folder_stats()).
begin;

create table id_ks3_entry (
    id bigserial primary key,
    description text not null,
    amount_rub numeric(15,2) not null,
    created_by bigint,
    created_at timestamptz not null default now(),
    updated_by bigint,
    updated_at timestamptz
);

commit;
