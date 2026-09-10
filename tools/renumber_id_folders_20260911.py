#!/usr/bin/env python3
"""
Заход 6, 11.09.2026, задача 2 — разовая перенумерация папок ИД по
хронологии создания. Тестовые папки сессий (id 10, 12, 13 — "НОЧНОЙ-
ТЕСТ", "НОЧНОЙ-ТЕСТ-2", "ЗАХОД3-ТЕСТ") к этому моменту уже удалены
самими же прогонами сразу после проверки (см. docs/NIGHT_RUN_20260909.md,
docs/RUN_3_20260910.md) — на 11.09.2026 в id_folder ни одной тестовой
папки НЕ осталось: все 15 существующих (id 1-9, 11, 14-18) имеют
реальные разделы (1-36 шт.), реальные суммы, создатель — именованный
пользователь `savchuk`, не тестовый/веб-форменный аккаунт. Задание
предполагало, что ТМ-009/ТМ-010 — тестовые; проверка (прямой запрос по
составу папок и создателю) этого не подтвердила — записано как
разошедшееся с ожиданием в docs/RUN_6_20260911.md, не подогнано под
предположение.

Поэтому перенумеровка — не "исключить тестовые, перенумеровать
реальные", а просто закрыть дыры в номерах (10, 12, 13 отсутствуют)
строго по `created_at` (что здесь совпадает с порядком `id`, так как
`id` — auto-increment, никогда не переносится вручную).

`name` — не первичный ключ (id_folder.id остаётся первичным ключом и
не трогается вообще), ограничения уникальности на `name` сейчас нет
(проверено `pg_constraint`), но переименование всё равно идёт через
промежуточные значения в одной транзакции — по прямому требованию
задания, не полагаясь на текущее отсутствие ограничения.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/renumber_id_folders_20260911.py
"""
import sys

sys.path.insert(0, "/app")
from db import query, run_in_transaction  # noqa: E402


def main():
    folders = query("select id, name, created_at from id_folder order by created_at, id")
    print(f"папок всего: {len(folders)}")
    for f in folders:
        print(f"  id={f['id']:>3}  {f['name']:<8}  создана {f['created_at']}")

    new_names = {f["id"]: f"ТМ-{i:03d}" for i, f in enumerate(folders, start=1)}
    changed = {fid: new for fid, new in new_names.items()
               if new != next(f["name"] for f in folders if f["id"] == fid)}
    print(f"\nбудет переименовано: {len(changed)} из {len(folders)}")
    for fid, new in sorted(changed.items()):
        old = next(f["name"] for f in folders if f["id"] == fid)
        print(f"  id={fid}: {old} -> {new}")

    def _do(cur):
        # Проход 1 — временные значения, гарантированно не пересекающиеся
        # ни с текущими, ни с целевыми именами.
        for f in folders:
            cur.execute("update id_folder set name=%s where id=%s", (f"TMP-{f['id']}", f["id"]))
        # Проход 2 — окончательные значения.
        for fid, new in new_names.items():
            cur.execute("update id_folder set name=%s where id=%s", (new, fid))

    run_in_transaction(_do)

    after = query("select id, name from id_folder order by id")
    print("\nитог:")
    for r in after:
        print(f"  id={r['id']:>3}  {r['name']}")


if __name__ == "__main__":
    main()
