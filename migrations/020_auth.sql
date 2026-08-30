-- Учётные записи и разграничение доступа. Документ «Ответственные по
-- разделам» от 29.08.2026. Логин/пароль привязываются к УЖЕ
-- существующему app_user (не заводим второй список фамилий — тот же
-- принцип, что просил координатор про downtime_cause/REASON_CODES).

alter table app_user
    add column login text unique,
    add column password_hash text,
    add column password_salt text,
    add column password_changed_at timestamptz;

-- Разрешения — один человек может иметь несколько (несколько вкладок
-- ИД + форма ИЗМ у Болтика, например). Общая модель на все виды
-- разрешений, не отдельная таблица на каждый вид (то же соображение
-- "не плодить справочники, что и просил координатор").
--   'id_tab:<code>'   — вкладка id_form_tab.code, доступ к вводу по ней
--   'changes:submit'  — форма ИЗМ
--   'prescriptions:submit' — форма «Предписания»
--   'admin'           — координатор, видит/делает всё, разрешения не проверяются
create table user_permission (
    id bigint generated always as identity primary key,
    user_id bigint not null references app_user(id),
    permission text not null,
    created_at timestamptz not null default now(),
    unique (user_id, permission)
);

-- Сессии — непрозрачный токен в cookie, хеш токена здесь (не сам
-- токен — тот же принцип, что и с паролем: утечка БД не должна отдавать
-- рабочие токены как есть).
create table user_session (
    token_hash text primary key,
    user_id bigint not null references app_user(id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    ip text,
    user_agent text
);
create index idx_user_session_user on user_session(user_id);
create index idx_user_session_expires on user_session(expires_at);

-- Журнал входов — отдельно от audit_log (тот про действия с данными,
-- этот про сам факт входа, включая неудачные попытки).
create table login_log (
    id bigint generated always as identity primary key,
    login_attempted text not null,
    user_id bigint references app_user(id),
    success boolean not null,
    reason text,
    ip text,
    user_agent text,
    created_at timestamptz not null default now()
);
create index idx_login_log_user on login_log(user_id);
create index idx_login_log_created on login_log(created_at);

-- audit_log.user_id уже был (см. \d audit_log) — просто теперь будет
-- реально заполняться логином вошедшего, а не общим web-form
-- пользователем. Записи ДО введения учёток остаются на общем
-- пользователе — не переписываем их (правило координатора).
