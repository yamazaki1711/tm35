#!/usr/bin/env python3
"""
Импорт v2 «График ИД» — группы вместо одной строки-раздела (часть 3,
09.09.2026). Заменяет tools/import_grafik_id_report_meta.py: та схема
(id_row_report_meta, row_id unique) не могла принять группу из
нескольких id_form_row на одну строку Excel ("Н1-4" = 5 строк БД) —
отсюда 17 из 133 в части 1, не 13% реальных расхождений данных.

Модель: id_report_group — создаётся ВСЕГДА, для всех 133 строк CSV
(метаданные участок/категория/тип/исполнитель/КС-2 не зависят от
сопоставления с id_form_row). Участники (id_report_group_row) —
отдельный, второй проход: разбор текста name на коды/диапазоны,
поиск кандидатов id_form_row по префиксу+номеру.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/import_grafik_id_groups.py

Заход 4, 10.09.2026: разбор кода/поиск кандидата вынесен в
backend/grafik_matching.py — см. её докстринг. Раньше был почти
дословно продублирован здесь и в tools/match_groups_v3.py.
"""
import csv
import sys

sys.path.insert(0, "/app")
from db import query, run_in_transaction  # noqa: E402
from grafik_matching import build_row_index, resolve_group_tokens  # noqa: E402

CSV_PATH = "/app/docs_import/grafik_id_extract_20260901.csv"
UNMATCHED_PATH = "/app/tools/import_grafik_id_unmatched_v2.csv"


def main():
    csv_rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    print(f"прочитано строк CSV: {len(csv_rows)}")

    idx = build_row_index(query)

    uchastok_order = []
    display_order_counter = {}
    groups = []  # каждая запись: dict с метаданными + resolved member row_ids + unmatched notes

    claimed_by = {}  # row_id -> group_source_row (для конфликтов "уже занят другой группой")
    unmatched_log = []

    for csv_row in csv_rows:
        uch_label = csv_row["uchastok"].strip()
        if uch_label not in uchastok_order:
            uchastok_order.append(uch_label)
        uchastok_no = uchastok_order.index(uch_label) + 1
        category = csv_row["category"].strip()
        key = (uchastok_no, category)
        display_order_counter[key] = display_order_counter.get(key, 0) + 1
        display_order = display_order_counter[key]

        cost_raw = (csv_row.get("cost_mln") or "").strip()
        cost_mln = float(cost_raw) if cost_raw else None
        source_row = int(csv_row["row"])
        group_label = csv_row["name"]

        raw_row_ids, reasons = resolve_group_tokens(idx, group_label, category)
        member_row_ids = set()
        for rid in raw_row_ids:
            if rid in claimed_by and claimed_by[rid] != source_row:
                reasons.append(f"раздел id={rid} уже занят группой из строки CSV {claimed_by[rid]}")
                continue
            claimed_by[rid] = source_row
            member_row_ids.add(rid)
        for reason in reasons:
            unmatched_log.append({"source_row": source_row, "group_label": group_label, "reason": reason})

        groups.append({
            "uchastok_no": uchastok_no, "uchastok_label": uch_label,
            "category_group": category,
            "group_label": group_label,
            "type_label": (csv_row.get("type") or "").strip() or None,
            "executor_name": (csv_row.get("executor") or "").strip() or None,
            "display_order": display_order,
            "ks2_cost_mln": cost_mln,
            "source_row": source_row,
            "member_row_ids": sorted(member_row_ids),
        })

    def _do(cur):
        for g in groups:
            cur.execute(
                """insert into id_report_group
                   (uchastok_no, uchastok_label, category_group, group_label, type_label,
                    executor_name, display_order, ks2_cost_mln, source_row)
                   values (%(uchastok_no)s, %(uchastok_label)s, %(category_group)s, %(group_label)s,
                           %(type_label)s, %(executor_name)s, %(display_order)s, %(ks2_cost_mln)s, %(source_row)s)
                   returning id""",
                g,
            )
            group_id = cur.fetchone()["id"]
            for rid in g["member_row_ids"]:
                cur.execute(
                    "insert into id_report_group_row (group_id, row_id) values (%s, %s)",
                    (group_id, rid),
                )

    run_in_transaction(_do)

    if unmatched_log:
        with open(UNMATCHED_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["source_row", "group_label", "reason"])
            w.writeheader()
            for u in unmatched_log:
                w.writerow(u)

    total_groups = len(groups)
    groups_with_members = sum(1 for g in groups if g["member_row_ids"])
    groups_empty = total_groups - groups_with_members
    total_members = sum(len(g["member_row_ids"]) for g in groups)

    print(f"групп создано: {total_groups} (всегда все строки CSV)")
    print(f"групп хотя бы с одним разделом: {groups_with_members}")
    print(f"групп без единого раздела: {groups_empty}")
    print(f"всего привязок раздел->группа: {total_members}")
    print(f"строк в unmatched_v2 (отдельные коды/конфликты): {len(unmatched_log)} -> {UNMATCHED_PATH}")


if __name__ == "__main__":
    main()
