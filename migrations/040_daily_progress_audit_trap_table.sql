-- Найдено случайно 17.09.2026 при проверке задачи 3 (откат тестовой
-- записи /shift): триггер `daily_progress_audit_trap_trg` (AFTER UPDATE
-- OR DELETE на daily_progress, вызывает daily_progress_audit_trap_fn())
-- существует и висит на таблице в проде, но таблицы
-- `daily_progress_audit_trap`, в которую он пишет старую строку перед
-- изменением/удалением, нет НИГДЕ — ни в БД, ни в единой миграции.
-- Результат: **любое** UPDATE или DELETE над daily_progress (в т.ч.
-- обычное "исправить уже введённый факт" через /shift или /gantt —
-- `on conflict ... do update`, если конфликт реально случился) падает
-- с ошибкой "relation does not exist" прямо в проде. INSERT без
-- конфликта не задевает триггер (AFTER UPDATE/DELETE, не INSERT) —
-- поэтому первый ввод факта на новую дату/работу проходит незамеченно,
-- а повторное исправление того же дня — нет.
--
-- Не часть задачи 3 по содержанию, но напрямую опасно оставлять как
-- есть (роняет ежедневный ввод факта у всей группы СМР, не только
-- то, что правит эта задача) — заводится таблица, под которую триггер
-- и функция уже написаны и рассчитаны (см. daily_progress_audit_trap_fn(),
-- столбцы читаются из её же текста), сам триггер не трогается.

begin;

create table if not exists daily_progress_audit_trap (
    id                bigint generated always as identity primary key,
    op                text not null,
    dp_id             bigint,
    old_row           jsonb,
    db_role           text,
    application_name  text,
    client_addr       inet,
    pid               integer,
    changed_at        timestamptz not null default now()
);
create index if not exists idx_daily_progress_audit_trap_dp_id on daily_progress_audit_trap(dp_id);

commit;
