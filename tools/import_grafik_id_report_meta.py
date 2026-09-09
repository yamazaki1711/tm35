#!/usr/bin/env python3
"""
Разовый импорт отчётной разметки "График ИД" (участок/категория/КС-2) из
docs/import/grafik_id_extract_20260901.csv в таблицу id_row_report_meta
(миграция 029). Не путать с постоянной моделью id_form_row/id_form_tab —
это отдельный отчётный слой, см. комментарий в самой миграции.

Сопоставление CSV-строки с id_form_row — по совпадению (без учёта
регистра, пробелов, переносов строк) поля `name` из CSV с полем
section_label ИЛИ construction_label строки id_form_row. Строго 1
кандидат — импорт. 0 или 2+ кандидатов — в unmatched, без догадок.

Важно (найдено при разборе, не бага ради, а по факту модели): в базе
многие "разделы" Excel-графика (например "Н1-4") соответствуют НЕСКОЛЬКИМ
строкам id_form_row с ОДИНАКОВЫМ section_label (по одной строке на
конструктивный элемент внутри диапазона — "Н1", "Н2", "Н3", "Н4"). Это
тоже считается неоднозначным совпадением (не 1 кандидат) и уходит в
unmatched: агрегация стоимости/статуса по группе — отдельное проектное
решение, не техническая деталь импорта, схема id_row_report_meta
рассчитана на ровно один row_id. См. docs/decisions_needed_grafik_id_export.md.

Запуск — ТОЛЬКО внутри контейнера tm_backend (там же, где живёт db.py и
переменная окружения TM35_DSN):
    docker exec tm_backend python3 tools/import_grafik_id_report_meta.py
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from db import query, run_in_transaction  # noqa: E402

CSV_PATH = "/app/docs_import/grafik_id_extract_20260901.csv"
UNMATCHED_PATH = "/app/tools/import_grafik_id_unmatched.csv"

UCHASTOK_NO_BY_LABEL = {}  # заполняется по факту первого встреченного текста участка, по порядку появления


def norm(s):
    if not s:
        return ""
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def main():
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    print(f"прочитано строк CSV: {len(rows)}")

    all_rows = query("select id, tab_id, section_label, construction_label from id_form_row")
    by_norm = {}
    for r in all_rows:
        for field in ("section_label", "construction_label"):
            key = norm(r[field])
            if key:
                by_norm.setdefault(key, set()).add(r["id"])
    tab_label_by_row = {}
    for r in query("select r.id as row_id, t.label as tab_label from id_form_row r join id_form_tab t on t.id=r.tab_id"):
        tab_label_by_row[r["row_id"]] = r["tab_label"]

    uchastok_order = []
    imported = []
    unmatched = []
    display_order_counter = {}

    for csv_row in rows:
        uch_label = csv_row["uchastok"].strip()
        if uch_label not in uchastok_order:
            uchastok_order.append(uch_label)
        uchastok_no = uchastok_order.index(uch_label) + 1

        category = csv_row["category"].strip()
        key = (uchastok_no, category)
        display_order_counter[key] = display_order_counter.get(key, 0) + 1
        display_order = display_order_counter[key]

        name_key = norm(csv_row["name"])
        candidates = sorted(by_norm.get(name_key, set()))

        cost_raw = (csv_row.get("cost_mln") or "").strip()
        cost_mln = float(cost_raw) if cost_raw else None
        source_row = int(csv_row["row"])

        if len(candidates) == 1:
            imported.append({
                "row_id": candidates[0],
                "uchastok_no": uchastok_no,
                "uchastok_label": uch_label,
                "category_group": category,
                "type_label": (csv_row.get("type") or "").strip() or None,
                "executor_name": (csv_row.get("executor") or "").strip() or None,
                "display_order": display_order,
                "ks2_cost_mln": cost_mln,
                "source_row": source_row,
            })
        else:
            unmatched.append({
                **csv_row,
                "normalized_name": name_key,
                "candidate_count": len(candidates),
                "candidate_ids": ";".join(str(c) for c in candidates),
                "candidate_tabs": ";".join(sorted(set(tab_label_by_row.get(c, "?") for c in candidates))),
            })

    # Однозначность row_id тоже проверяем на своей стороне (UNIQUE в БД её
    # обеспечит, но лучше явно — на случай, если один и тот же row_id
    # случайно однозначно совпал с двумя разными строками CSV).
    seen_row_ids = {}
    final_imported = []
    for item in imported:
        rid = item["row_id"]
        if rid in seen_row_ids:
            prev = seen_row_ids[rid]
            print(f"ВНИМАНИЕ: row_id={rid} однозначно совпал с двумя строками CSV "
                  f"(source_row={prev['source_row']} и {item['source_row']}) — оставляю первую, "
                  f"вторую увожу в unmatched вручную-логически (не должно происходить, но не молчим).")
            continue
        seen_row_ids[rid] = item
        final_imported.append(item)

    if final_imported:
        writes = [
            {
                "row_id": it["row_id"],
                "uchastok_no": it["uchastok_no"],
                "uchastok_label": it["uchastok_label"],
                "category_group": it["category_group"],
                "type_label": it["type_label"],
                "executor_name": it["executor_name"],
                "display_order": it["display_order"],
                "ks2_cost_mln": it["ks2_cost_mln"],
                "source_row": it["source_row"],
            }
            for it in final_imported
        ]

        def _do(cur):
            for w in writes:
                cur.execute(
                    """insert into id_row_report_meta
                       (row_id, uchastok_no, uchastok_label, category_group, type_label, executor_name,
                        display_order, ks2_cost_mln, source_row)
                       values (%(row_id)s, %(uchastok_no)s, %(uchastok_label)s, %(category_group)s, %(type_label)s,
                               %(executor_name)s, %(display_order)s, %(ks2_cost_mln)s, %(source_row)s)""",
                    w,
                )

        run_in_transaction(_do)

    if unmatched:
        with open(UNMATCHED_PATH, "w", encoding="utf-8", newline="") as f:
            fieldnames = list(rows[0].keys()) + ["normalized_name", "candidate_count", "candidate_ids", "candidate_tabs"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for u in unmatched:
                w.writerow(u)

    print(f"импортировано: {len(final_imported)}")
    print(f"unmatched: {len(unmatched)} -> {UNMATCHED_PATH}")
    print(f"участков распознано: {len(uchastok_order)}")
    for i, u in enumerate(uchastok_order, 1):
        print(f"  №{i}: {u}")


if __name__ == "__main__":
    main()
