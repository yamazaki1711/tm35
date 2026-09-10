-- Заход 3, 10.09.2026, задача 4. Связь раздела ИД с замечанием РСК,
-- препятствующим принятию ИД — сейчас не существует вовсе, поэтому
-- колонка "Замеч. РСК" в экспорте «График ИД» всегда пустая.
--
-- Отклонение от буквального текста задания: задание говорит "join table
-- (rsk_processing <-> id_form_row)", но rsk_processing существует
-- только для 56 из 152 нарушений (rsk_processing.violation_id -
-- начатая отработка, не сама запись о нарушении) — привязка к ней
-- лишила бы ПТО возможности прикрепить 96 из 152 реальных нарушений.
-- Связываем с rsk_violation напрямую (есть у каждого нарушения,
-- адресуется тем же sys_no, что везде в контуре РСК) — записано в
-- docs/RUN_3_20260910.md как осознанное отклонение, не молчаливая
-- замена.
begin;

create table id_row_rsk_link (
    id bigserial primary key,
    row_id bigint not null references id_form_row(id),
    violation_id bigint not null references rsk_violation(id),
    created_by bigint,
    created_at timestamptz not null default now(),
    unique (row_id, violation_id)
);

create index id_row_rsk_link_row_id_idx on id_row_rsk_link(row_id);
create index id_row_rsk_link_violation_id_idx on id_row_rsk_link(violation_id);

commit;
