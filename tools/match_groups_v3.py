#!/usr/bin/env python3
"""
Ночной прогон 09-10.09.2026, задача 5 — вторая попытка сопоставления
для групп, оставшихся БЕЗ единого раздела после части 3 (81 из 133 на
момент запуска). Не трогает уже заполненные группы, не создаёт новых
групп (это делает tools/import_grafik_id_groups.py) — только пробует
доукомплектовать пустые.

Что нового по сравнению с частью 3 (см. decisions_needed п.18):
подсказка "категория CSV -> вкладка" расширена с одного проверенного
случая ("Обвязка камер" -> "Обвязка") до общего правила: если название
вкладки (например "Камеры") целиком входит текстом в категорию группы
(например "Камеры" или "Обвязка камер") — и это единственная вкладка
среди коллизии, для которой это верно, — используем её. Для "Камеры и
колодцы" правило НЕ срабатывает (совпадает срузу с "Камеры" И
"Колодцы" — остаётся неоднозначным, не гадаем).

Пишет ОТЧЁТ (docs/match_groups_v3_report.md), не тихую запись в БД:
сколько было/стало, что удалось разрешить и почему, что осталось
нерешённым и почему. Запись в id_report_group_row — только для
однозначных случаев.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/match_groups_v3.py

Заход 4, 10.09.2026: разбор кода/поиск кандидата вынесен в
backend/grafik_matching.py — тот же текст теперь используется этим
скриптом, tools/import_grafik_id_groups.py и новым экраном
category→tabs (main.py). Раньше был почти дословно продублирован
здесь и в import_grafik_id_groups.py.
"""
import sys

sys.path.insert(0, "/app")
from db import query, query_one, run_in_transaction  # noqa: E402
from grafik_matching import (  # noqa: E402
    build_row_index, resolve_group_tokens,
)

REPORT_PATH = "/app/docs_report/match_groups_v3_report.md"


def main():
    empty_groups = query("""
        select g.id, g.source_row, g.group_label, g.category_group
        from id_report_group g
        left join id_report_group_row gr on gr.group_id = g.id
        where gr.group_id is null
        order by g.source_row
    """)
    print(f"групп без единого раздела на начало: {len(empty_groups)}")

    already_claimed = {r["row_id"] for r in query("select row_id from id_report_group_row")}
    idx = build_row_index(query)

    resolved = []
    still_unresolved = []

    for g in empty_groups:
        raw_row_ids, reasons = resolve_group_tokens(idx, g["group_label"], g["category_group"])
        member_row_ids = set()
        for rid in raw_row_ids:
            if rid in already_claimed:
                reasons.append(f"раздел id={rid} уже занят другой группой")
                continue
            member_row_ids.add(rid)

        if member_row_ids:
            resolved.append({"group": g, "row_ids": sorted(member_row_ids), "partial_reasons": reasons})
        else:
            still_unresolved.append({"group": g, "reasons": reasons})

    def _do(cur):
        for r in resolved:
            for rid in r["row_ids"]:
                cur.execute(
                    "insert into id_report_group_row (group_id, row_id) values (%s, %s) on conflict do nothing",
                    (r["group"]["id"], rid),
                )
                already_claimed.add(rid)

    if resolved:
        run_in_transaction(_do)

    total_after = query_one("""
        select count(*) as n from id_report_group g
        where exists (select 1 from id_report_group_row gr where gr.group_id = g.id)
    """)["n"]

    lines = []
    lines.append("# Отчёт второй попытки сопоставления групп (v3) — ночной прогон, задача 5")
    lines.append("")
    lines.append(f"Групп без единого раздела на старте: **{len(empty_groups)}**.")
    lines.append(f"Из них доукомплектовано в этом проходе: **{len(resolved)}**.")
    lines.append(f"Осталось без единого раздела: **{len(still_unresolved)}**.")
    lines.append(f"Всего групп хотя бы с одним разделом теперь: **{total_after}** из 133.")
    lines.append("")
    lines.append("Новое в v3 по сравнению с частью 3: подсказка «категория → вкладка» "
                  "обобщена с единственного проверенного случая («Обвязка камер» → «Обвязка») "
                  "до общего правила — название вкладки, целиком входящее в текст категории, "
                  "берётся как подсказка, если это единственная такая вкладка среди коллизии. "
                  "«Камеры и колодцы» под это не подпадает (задевает сразу «Камеры» и «Колодцы») — "
                  "остаётся неоднозначным намеренно, не гадаем.")
    lines.append("")
    lines.append("## Доукомплектованные группы")
    lines.append("")
    if resolved:
        lines.append("| Строка | Группа | Категория | Добавлено разделов |")
        lines.append("|---|---|---|---|")
        for r in resolved:
            g = r["group"]
            lines.append(f"| {g['source_row']} | {g['group_label']} | {g['category_group']} | {len(r['row_ids'])} |")
    else:
        lines.append("(ни одной)")
    lines.append("")
    lines.append("## Осталось без единого раздела — с причиной")
    lines.append("")
    lines.append("| Строка | Группа | Категория | Причины |")
    lines.append("|---|---|---|---|")
    for u in still_unresolved:
        g = u["group"]
        reasons_text = "; ".join(u["reasons"][:5]) if u["reasons"] else "(токены не разобрались вовсе)"
        lines.append(f"| {g['source_row']} | {g['group_label']} | {g['category_group']} | {reasons_text} |")

    import os
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"доукомплектовано групп: {len(resolved)}")
    print(f"осталось без раздела: {len(still_unresolved)}")
    print(f"всего с разделами теперь: {total_after} из 133")
    print(f"отчёт: {REPORT_PATH}")


if __name__ == "__main__":
    main()
