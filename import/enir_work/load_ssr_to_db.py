"""
Загрузка справочника СТО-ССР-2026 (Spider Project) в таблицу ssr_norm
БД tm35. Запускается внутри контейнера tm_backend (psycopg2, TM35_DSN
уже в окружении).
"""
import json
import os

import psycopg2
import psycopg2.extras


def main():
    src = os.environ.get("SSR_JSON", "/opt/kat/tm-app/ssr_spider_norms.json")
    ops = json.load(open(src, encoding="utf-8"))

    rows = []
    for o in ops:
        rows.append((
            o["section"], o["code"], o["name"], o.get("unit"),
            o.get("team_productivity_per_hour"), o.get("labor_hours_per_unit"),
            psycopg2.extras.Json(o["crew"]), o.get("notes"),
        ))

    dsn = os.environ["TM35_DSN"]
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("select count(*) from ssr_norm")
            before = cur.fetchone()[0]
            # code не уникален в источнике (см. migrations/009) — простой
            # INSERT, без ON CONFLICT; повторный запуск на непустой
            # таблице удвоит строки, поэтому чистим перед загрузкой.
            cur.execute("delete from ssr_norm")
            psycopg2.extras.execute_batch(
                cur,
                """
                insert into ssr_norm (section, code, name, unit, team_productivity_per_hour,
                                       labor_hours_per_unit, crew, notes)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("select count(*), count(labor_hours_per_unit) from ssr_norm")
            total, with_hours = cur.fetchone()
    finally:
        conn.close()

    print(f"было строк: {before}")
    print(f"загружено/обновлено: {len(rows)}")
    print(f"итого в таблице: {total}, с трудоёмкостью: {with_hours}")


if __name__ == "__main__":
    main()
