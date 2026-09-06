#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Загрузка parsed_act.json (см. parse_act_pdf.py) в БД tm35 —
rsk_act/rsk_violation/rsk_act_item. Идемпотентно: upsert по act_no и
sys_no, повторный запуск не плодит дубли.

Запуск: TM35_DSN=... python3 load_act.py
"""
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

DSN = os.environ.get("TM35_DSN") or os.environ.get("DATABASE_URL")
IN_PATH = os.environ.get("RSK_PARSED_ACT_JSON", "/home/oleg/Documents/TM-35/import/rsk/parsed_act.json")


def main():
    if not DSN:
        print("TM35_DSN/DATABASE_URL не задан", file=sys.stderr)
        sys.exit(1)

    with open(IN_PATH, encoding="utf-8") as f:
        data = json.load(f)

    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    act = data["act"]
    cur.execute(
        "insert into rsk_act (act_no, act_date, total_declared, pdf_path) values (%s,%s,%s,%s) "
        "on conflict (act_no) do update set act_date=excluded.act_date, "
        "total_declared=excluded.total_declared, pdf_path=excluded.pdf_path "
        "returning id",
        (act["act_no"], act["act_date"], act["total_declared"], act["pdf_path"]),
    )
    act_id = cur.fetchone()["id"]

    n_violation_inserted = n_violation_updated = 0
    n_item_inserted = n_item_updated = 0
    for rec in data["records"]:
        cur.execute(
            """
            insert into rsk_violation (sys_no, first_act_no, first_detected_date)
            values (%(sys_no)s, %(first_act_no)s, %(first_detected_date)s)
            on conflict (sys_no) do update set
                -- Дата/акт первого обнаружения — фиксируются один раз; если
                -- будущий акт вдруг покажет более раннюю дату (не должно, но
                -- не гадаем), берём меньшую, а не слепо перезаписываем.
                first_detected_date = least(rsk_violation.first_detected_date, excluded.first_detected_date),
                first_act_no = case
                    when excluded.first_detected_date < rsk_violation.first_detected_date
                    then excluded.first_act_no else rsk_violation.first_act_no end
            returning id, (xmax = 0) as inserted
            """,
            rec,
        )
        row = cur.fetchone()
        violation_id = row["id"]
        if row["inserted"]:
            n_violation_inserted += 1
        else:
            n_violation_updated += 1

        cur.execute(
            """
            insert into rsk_act_item
                (act_id, violation_id, item_no, control_section, content, remedy, due_date, is_repeat)
            values (%(act_id)s, %(violation_id)s, %(item_no)s, %(control_section)s, %(content)s,
                    %(remedy)s, %(due_date)s, %(is_repeat)s)
            on conflict (act_id, violation_id) do update set
                item_no=excluded.item_no, control_section=excluded.control_section,
                content=excluded.content, remedy=excluded.remedy, due_date=excluded.due_date,
                is_repeat=excluded.is_repeat
            returning id, (xmax = 0) as inserted
            """,
            {**rec, "act_id": act_id, "violation_id": violation_id},
        )
        item_row = cur.fetchone()
        if item_row["inserted"]:
            n_item_inserted += 1
        else:
            n_item_updated += 1

    conn.commit()

    print(f"rsk_act: id={act_id}")
    print(f"rsk_violation: {n_violation_inserted} новых, {n_violation_updated} обновлено")
    print(f"rsk_act_item: {n_item_inserted} новых, {n_item_updated} обновлено")

    cur.execute("select count(*) as n from rsk_violation")
    print("Итого rsk_violation:", cur.fetchone()["n"])
    cur.execute("select count(*) as n from rsk_act_item where act_id=%s", (act_id,))
    print("Итого rsk_act_item (этот акт):", cur.fetchone()["n"])
    cur.execute("select count(*) as n from rsk_act_item where act_id=%s and remedy is null", (act_id,))
    print("Без remedy:", cur.fetchone()["n"])
    cur.execute("select count(*) as n from rsk_act_item where act_id=%s and due_date is null", (act_id,))
    print("Без due_date:", cur.fetchone()["n"])
    cur.execute("select count(*) as n from rsk_violation where first_act_no is null")
    print("Без first_act_no:", cur.fetchone()["n"])

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
