"""
Загрузка справочника норм ГЭСН (gesn_norms.json, уже проверен вручную,
см. docs/spravochnik_GESN_TM35.md) в таблицу gesn_norm БД tm35.
Запускается на kat-core (использует TM35_DSN из окружения контейнера).
"""
import json
import os

import psycopg2

SBORNIK_TITLES = {
    "GESN_01_Zemlyanye": "ГЭСН 81-02-01-2022 Сборник 1. Земляные работы",
    "GESN_05_Svainye": "ГЭСН 81-02-05-2022 Сборник 5. Свайные работы, опускные колодцы, закрепление грунтов",
    "GESN_06_Beton_monolit": "ГЭСН 81-02-06-2022 Сборник 6. Бетонные и железобетонные конструкции монолитные",
    "GESN_09_Metallokonstrukcii": "ГЭСН 81-02-09-2022 Сборник 9. Строительные металлические конструкции",
    "GESN_13_Korroziya": "ГЭСН 81-02-13-2022 Сборник 13. Защита строительных конструкций и оборудования от коррозии",
    "GESN_24_Teplosnabzhenie": "ГЭСН 81-02-24-2022 Сборник 24. Теплоснабжение и газопроводы - наружные сети",
    "GESN_26_Teploizolyaciya": "ГЭСН 81-02-26-2022 Сборник 26. Теплоизоляционные работы",
    "GESN_27_Dorogi": "ГЭСН 81-02-27-2022 Сборник 27. Автомобильные дороги",
    "GESN_33_LEP": "ГЭСН 81-02-33-2022 Сборник 33. Линии электропередачи",
    "GESN_47_Ozelenenie": "ГЭСН 81-02-47-2022 Сборник 47. Озеленение, защитные лесонасаждения",
    "GESNm_08_Elektrotekhn": "ГЭСНм 81-03-08-2022 Сборник 8. Электротехнические установки",
    "GESNm_12_Tekhnolog_truboprovody": "ГЭСНм 81-03-12-2022 Сборник 12. Технологические трубопроводы",
}


def main():
    src = os.environ.get("GESN_JSON", "/opt/kat/tm-app/gesn_norms.json")
    groups = json.load(open(src, encoding="utf-8"))

    # Один и тот же (sbornik, code) изредка встречается в источнике дважды
    # (напр. таблица норм и отдельная таблица коэффициентов/примечаний,
    # ссылающаяся на тот же диапазон кодов) — 63 таких пары нашлось на
    # 10767 строк. НАЙДЕННЫЙ БАГ (19.08): при простом executemany с
    # ON CONFLICT DO UPDATE вторая (обычно пустая, hours_per_unit=None)
    # запись затирала уже извлечённое число — проверено на живой БД
    # (01-01-144-01/-02 стали NULL после первой загрузки). Дедуп здесь,
    # ДО отправки в БД: из дублей берём тот, где число есть.
    by_key = {}
    for g in groups:
        sb_code = g["sbornik"]
        sb_title = SBORNIK_TITLES.get(sb_code, sb_code)
        group_title = (g.get("group_title") or "").strip()
        for c in g["codes"]:
            name = c["name"]
            if group_title and group_title not in name:
                name = f"{group_title}: {name}"
            key = (sb_code, c["code"])
            row = (sb_code, sb_title, c["code"], name, c.get("unit"), c["hours_per_unit"])
            if key not in by_key or (by_key[key][5] is None and row[5] is not None):
                by_key[key] = row
    rows = list(by_key.values())

    dsn = os.environ["TM35_DSN"]
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("select count(*) from gesn_norm")
            before = cur.fetchone()[0]
            cur.executemany(
                """
                insert into gesn_norm (sbornik_code, sbornik_title, code, name, unit, hours_per_unit)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (sbornik_code, code) do update set
                    sbornik_title = excluded.sbornik_title,
                    name = excluded.name,
                    unit = excluded.unit,
                    hours_per_unit = excluded.hours_per_unit
                """,
                rows,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("select count(*), count(hours_per_unit) from gesn_norm")
            total, with_hours = cur.fetchone()
    finally:
        conn.close()

    print(f"было строк: {before}")
    print(f"загружено/обновлено: {len(rows)}")
    print(f"итого в таблице: {total}, с нормой чел-час: {with_hours}")


if __name__ == "__main__":
    main()
