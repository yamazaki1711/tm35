#!/usr/bin/env python3
"""
Убрать номера-остатки импорта из Excel в id_form_tab.label — задание
координатора, 11.09.2026. Меняет ТОЛЬКО label (отображение), code
(идентификатор, используется в запросах и в сопоставлении категорий)
не трогается.

Проверил все 15 вкладок (кроме служебных opv/n, но и их тоже смотрел)
прямым запросом перед правкой — только 1 ("1. ОПН") и 2 ("2. ОПН
(рсм)") несут числовой префикс; ни одна из остальных 13 не несёт
постороннего префикса/суффикса/пробела (сверено по repr(), не только
визуально).

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/fix_id_tab_labels_20260911.py
"""
import sys

sys.path.insert(0, "/app")
from db import query, run_in_transaction  # noqa: E402

RENAMES = {
    "opn": "ОПН",
    "opn_rsm": "ОПН (рсм)",
}


def main():
    before = query("select id, code, label from id_form_tab order by id")
    print("до правки:")
    for r in before:
        print(f"  id={r['id']:>2}  {r['code']:<14}  {r['label']!r}")

    def _do(cur):
        for code, new_label in RENAMES.items():
            cur.execute("update id_form_tab set label=%s where code=%s", (new_label, code))

    run_in_transaction(_do)

    after = query("select id, code, label from id_form_tab order by id")
    print("\nпосле правки:")
    for r in after:
        print(f"  id={r['id']:>2}  {r['code']:<14}  {r['label']!r}")


if __name__ == "__main__":
    main()
