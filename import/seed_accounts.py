"""
Разовый скрипт — создать 8 учётных записей по документу «Ответственные
по разделам» от 29.08.2026. Хеш пароля — тот же алгоритм, что в
main.py (hash_password/verify_password), продублирован здесь намеренно
(разовый скрипт, не хочет тянуть весь main.py с его FastAPI-инициализацией
ради одной функции).

Логин связывается с УЖЕ существующим app_user, где full_name совпадает
(создаётся новый app_user, если такого имени там ещё нет — двух Денисовых
не заводим, ищем по точному имени сначала).

Идемпотентно: повторный запуск обновляет пароль/разрешения, а не плодит
дубли (upsert по login).
"""
import hashlib
import os
import secrets
import sys

sys.path.insert(0, "/app")
from db import query_one, execute, execute_returning  # noqa: E402

ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz"  # без 0,1,l,o — визуально похожих
PBKDF2_ITERATIONS = 200_000


def gen_password():
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16)
    pw_norm = password.strip().lower()
    dk = hashlib.pbkdf2_hmac("sha256", pw_norm.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return dk.hex(), salt.hex()


ACCOUNTS = [
    # (ФИО, логин, вкладки ИД (id_form_tab.code) + доп. разрешения)
    # Координатор — не из документа "Ответственные по разделам", но без
    # него некому будет открывать /settings/users (смена паролей) после
    # снятия basic-auth со всего сайта. role='admin' — доступ ко всему,
    # разрешения по вкладкам не проверяются (см. is_admin() в main.py).
    ("Координатор", "admin", ["admin"]),
    ("Денисов М.В.", "denisov", ["id_tab:opn", "id_tab:opn_rsm", "id_tab:lotki"]),
    ("Перфилов А.В.", "perfilov", ["id_tab:truba_svarka"]),
    ("Свиренков А.В.", "svirenkov", ["id_tab:skolzyachki"]),
    ("Завгородний А.В.", "zavgorodniy", ["id_tab:obvyazka", "id_tab:met_konstr"]),
    ("Болтик В.Н.", "boltik", [
        "id_tab:no_v_lotkah", "id_tab:kamery", "id_tab:kolodcy",
        "id_tab:transhei", "id_tab:pavilyony", "changes:submit",
    ]),
    ("Савчук Е.А.", "savchuk", ["id_tab:sodk", "id_tab:izolyaciya"]),
    ("Посяда И.А.", "posyada", ["id_tab:elektrika"]),
    ("Мельников В.М.", "melnikov", ["prescriptions:submit"]),
]


def seed_missing_responsible():
    """Завгородний А.В. и Посяда И.А. отсутствуют в id_form_responsible
    (загружен из Excel раньше, документ от 29.08 их туда не добавлял) —
    координатор прямо просил добавить, не заводя второй справочник.
    Роль — общая "Ответственный за ввод данных" (в документе не указано,
    какой конкретно журнал/АОСР/ИС они ведут, только что они отвечают за
    вкладку в веб-форме) — не выдумываю конкретный журнал."""
    to_add = [
        ("obvyazka", "Завгородний А.В."),
        ("met_konstr", "Завгородний А.В."),
        ("elektrika", "Посяда И.А."),
    ]
    role = "Ответственный за ввод данных"
    for tab_code, full_name in to_add:
        tab = query_one("select id from id_form_tab where code=%s", (tab_code,))
        if not tab:
            print(f"  ПРОПУЩЕНО: вкладка {tab_code} не найдена")
            continue
        exists = query_one(
            "select id from id_form_responsible where tab_id=%s and full_name=%s and role=%s",
            (tab["id"], full_name, role),
        )
        if exists:
            continue
        max_order = query_one(
            "select coalesce(max(display_order), 0) as m from id_form_responsible where tab_id=%s",
            (tab["id"],),
        )["m"]
        execute(
            "insert into id_form_responsible (tab_id, role, full_name, display_order) values (%s, %s, %s, %s)",
            (tab["id"], role, full_name, max_order + 1),
        )
        print(f"  добавлено в id_form_responsible: {tab_code} — {full_name}")


def main():
    seed_missing_responsible()
    results = []
    for full_name, login, permissions in ACCOUNTS:
        row = query_one("select id from app_user where full_name=%s", (full_name,))
        if row:
            user_id = row["id"]
        else:
            role = "admin" if "admin" in permissions else "executor"
            user_id = execute_returning(
                "insert into app_user (full_name, role, is_active) values (%s, %s, true) returning id",
                (full_name, role),
            )["id"]

        password = gen_password()
        pw_hash, salt = hash_password(password)
        execute(
            "update app_user set login=%s, password_hash=%s, password_salt=%s, password_changed_at=now() where id=%s",
            (login, pw_hash, salt, user_id),
        )

        execute("delete from user_permission where user_id=%s", (user_id,))
        for perm in permissions:
            execute(
                "insert into user_permission (user_id, permission) values (%s, %s) on conflict do nothing",
                (user_id, perm),
            )

        results.append((full_name, login, password))

    print(f"{'ФИО':20s} {'логин':14s} {'пароль':10s}")
    for full_name, login, password in results:
        print(f"{full_name:20s} {login:14s} {password:10s}")


if __name__ == "__main__":
    main()
