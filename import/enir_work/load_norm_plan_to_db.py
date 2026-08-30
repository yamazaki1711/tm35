"""
Загрузка 56 нормированных позиций сметы в norm_plan_item. Источник —
smeta_normalized.json (matched_code заполнен), исключена вручную
найденная плохая пара n=270 ("Прокладка трубы" -> "Прокладка ВОЛС",
см. docs/smeta_normalization_test_2026-08-19.md).
"""
import json
import os

import psycopg2

BAD_MATCHES = {270}


def main():
    src = os.environ.get("SMETA_NORM_JSON", "/opt/kat/tm-app/smeta_normalized.json")
    rows_raw = json.load(open(src, encoding="utf-8"))
    rows = [r for r in rows_raw if r.get("matched_code") and r["n"] not in BAD_MATCHES]

    dsn = os.environ["TM35_DSN"]
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("delete from norm_plan_item")
            for r in rows:
                cur.execute(
                    """
                    insert into norm_plan_item
                        (smeta_n, name, unit, qty, matched_source, matched_code,
                         matched_name, hours_per_unit, labor_hours_total)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (r["n"], r["name"], r.get("unit"), r.get("qty"), r["matched_source"],
                     r["matched_code"], r["matched_name"], r["hours_per_unit"], r["labor_hours_total"]),
                )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("select count(*) from norm_plan_item")
            total = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"Загружено позиций: {len(rows)}")
    print(f"Итого в таблице: {total}")


if __name__ == "__main__":
    main()
