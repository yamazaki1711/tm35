#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Загрузка parsed.json (см. parse_rsk.py) в БД tm35. Идемпотентно: справочники
через "on conflict do nothing", rsk_violation через upsert по sys_no —
повторный запуск не плодит дубли, только обновляет уже вставленные записи.

Запуск: TM35_DSN=... python3 load_rsk.py
"""
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

DSN = os.environ.get("TM35_DSN") or os.environ.get("DATABASE_URL")
IN_PATH = os.environ.get("RSK_PARSED_JSON", "/home/oleg/Documents/TM-35/import/rsk/parsed.json")

CONTROL_MEASURES = [
    ("vhodnoy", "2.Проверка полноты и соблюдения установленных сроков выполнения подрядчиком входного "
                "контроля и достоверности документирования его результатов."),
    ("skladirovanie", "3.Проверка выполнения подрядчиком контрольных мероприятий по соблюдению правил "
                       "складирования и хранения применяемой продукции и достоверности документирования "
                       "его результатов."),
    ("posledovatelnost", "4.Проверка полноты и соблюдения установленных сроков выполнения подрядчиком "
                          "контроля последовательности и состава технологических операций по осуществлению "
                          "строительства объектов капитального строительства и достоверности "
                          "документирования."),
    ("inoe", "7.Проверка иных мероприятий в целях осуществления строительного контроля, предусмотренные "
             "законодательством Российской Федерации и (или) заключенным договором."),
]

CLOSE_CONDITIONS = [
    ("ид_принятие", "Неделя после принятия РСК соответствующих разделов ИД"),
    ("приказ_икс_рд", "Неделя после выхода приказа ИКС о внесении изменений в РД (ПД)"),
    ("работы_стройплощадка", "Неделя после выполнения Стр.площадкой работ"),
    ("освобождение_склад", "Неделя после завершения работ и освобождения складской площадки"),
    ("протоколы_уплотнения", "Неделя после получения протоколов уплотнения"),
    ("техрешение_дпр", "Неделя после завершения работ по техрешению ДПР, внесённому в РД"),
    ("геодезия", "Выполнение будет проверяться РСК после получения геодезической съёмки"),
    ("аоср_икс", "После подписания АОСР представителем ИКС"),
]

RESPONSIBLE_NAMES = ["ПТО", "ДПР", "Стройка ТМ-35", "ИКС", "Лаборатория"]


def main():
    if not DSN:
        print("TM35_DSN/DATABASE_URL не задан", file=sys.stderr)
        sys.exit(1)

    with open(IN_PATH, encoding="utf-8") as f:
        data = json.load(f)

    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ---- справочники ----
    cm_id = {}
    for code, label in CONTROL_MEASURES:
        cur.execute(
            "insert into rsk_control_measure (code, label) values (%s,%s) "
            "on conflict (code) do update set label=excluded.label returning id",
            (code, label),
        )
        cm_id[code] = cur.fetchone()["id"]
    # текст control_measure_raw из записи -> код: по номеру в начале строки ("2."/"3."/"4."/"7.")
    cm_by_prefix = {}
    for code, label in CONTROL_MEASURES:
        prefix = label.split(".", 1)[0] + "."
        cm_by_prefix[prefix] = code

    cc_id = {}
    for code, label in CLOSE_CONDITIONS:
        cur.execute(
            "insert into rsk_close_condition (code, label) values (%s,%s) "
            "on conflict (code) do update set label=excluded.label returning id",
            (code, label),
        )
        cc_id[code] = cur.fetchone()["id"]

    resp_id = {}
    for name in RESPONSIBLE_NAMES:
        cur.execute(
            "insert into rsk_responsible (name) values (%s) on conflict (name) do nothing returning id",
            (name,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("select id from rsk_responsible where name=%s", (name,))
            row = cur.fetchone()
        resp_id[name] = row["id"]

    act_meta = data["act"]
    cur.execute(
        "insert into rsk_act (act_no, act_date, total_violations, pdf_path) values (%s,%s,%s,%s) "
        "on conflict (act_no) do update set act_date=excluded.act_date, "
        "total_violations=excluded.total_violations, pdf_path=excluded.pdf_path returning id",
        (act_meta["act_no"], act_meta["act_date"], act_meta["total_violations"], act_meta["pdf_path"]),
    )
    main_act_id = cur.fetchone()["id"]

    # Прочие акты, известные только по колонкам реестра "Включён впервые/
    # в последний раз" — без total_violations/pdf_path (нет исходного PDF).
    other_act_id = {}

    def get_or_create_act(act_no, act_date):
        if act_no == act_meta["act_no"]:
            return main_act_id
        if act_no in other_act_id:
            return other_act_id[act_no]
        cur.execute(
            "insert into rsk_act (act_no, act_date) values (%s,%s) "
            "on conflict (act_no) do nothing returning id",
            (act_no, act_date),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("select id from rsk_act where act_no=%s", (act_no,))
            row = cur.fetchone()
        other_act_id[act_no] = row["id"]
        return row["id"]

    # ---- нарушения ----
    n_inserted = n_updated = 0
    violation_id_by_sysno = {}
    for rec in data["records"]:
        cm = cm_by_prefix.get((rec["control_measure_raw"] or "")[:2]) if rec["control_measure_raw"] else None
        cm_ref = cm_id.get(cm)
        cc_ref = cc_id.get(rec["close_condition_code"]) if rec["close_condition_code"] else None

        cur.execute(
            """
            insert into rsk_violation
                (sys_no, control_measure_id, content, remedy, violation_type, section_raw, is_repeat,
                 created_at, due_date, due_date_moved, urgent, author, state, closed_date,
                 close_condition_id, note, source,
                 track_phys, track_phys_raw, track_design, track_design_raw, track_id, track_id_raw,
                 updated_ts)
            values (%(sys_no)s, %(cm_ref)s, %(content)s, %(remedy)s, %(violation_type)s, %(section_raw)s,
                    %(is_repeat)s, %(created_at)s, %(due_date)s, %(due_date_moved)s, %(urgent)s, %(author)s,
                    %(state)s, %(closed_date)s, %(cc_ref)s, %(note)s, %(source)s,
                    %(track_phys)s, %(track_phys_raw)s, %(track_design)s, %(track_design_raw)s,
                    %(track_id)s, %(track_id_raw)s, now())
            on conflict (sys_no) do update set
                control_measure_id=excluded.control_measure_id, content=excluded.content,
                remedy=excluded.remedy, violation_type=excluded.violation_type,
                section_raw=excluded.section_raw, is_repeat=excluded.is_repeat,
                created_at=excluded.created_at, due_date=excluded.due_date,
                due_date_moved=excluded.due_date_moved, urgent=excluded.urgent, author=excluded.author,
                state=excluded.state, closed_date=excluded.closed_date,
                close_condition_id=excluded.close_condition_id, note=excluded.note, source=excluded.source,
                track_phys=excluded.track_phys, track_phys_raw=excluded.track_phys_raw,
                track_design=excluded.track_design, track_design_raw=excluded.track_design_raw,
                track_id=excluded.track_id, track_id_raw=excluded.track_id_raw,
                updated_ts=now()
            returning id, (xmax = 0) as inserted
            """,
            {**rec, "cm_ref": cm_ref, "cc_ref": cc_ref},
        )
        row = cur.fetchone()
        violation_id_by_sysno[rec["sys_no"]] = row["id"]
        if row["inserted"]:
            n_inserted += 1
        else:
            n_updated += 1

        # m2m ответственные — пересобираем целиком на каждый запуск (идемпотентно).
        cur.execute("delete from rsk_violation_responsible where violation_id=%s", (row["id"],))
        for name in rec["responsible"]:
            rid = resp_id.get(name)
            if rid:
                cur.execute(
                    "insert into rsk_violation_responsible (violation_id, responsible_id) values (%s,%s) "
                    "on conflict do nothing",
                    (row["id"], rid),
                )

        # rsk_act_item — из колонок "Включён впервые/в последний раз" (только реестровые записи).
        for raw_key in ("act_first_raw", "act_last_raw"):
            ref = rec.get(raw_key)
            if not ref:
                continue
            import re as _re
            # act_first_raw/act_last_raw уже содержат исходный текст "№4183-100, 27.01.2025" —
            # парсим здесь же, чтобы не тащить ещё одну колонку из parse_rsk.py.
            m2 = _re.search(r"(4183-\d+)\s*,\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})", ref)
            if not m2:
                continue
            act_no = m2.group(1)
            y = int(m2.group(4))
            y = y + 2000 if y < 100 else y
            act_date = "%04d-%02d-%02d" % (y, int(m2.group(3)), int(m2.group(2)))
            act_id = get_or_create_act(act_no, act_date)
            cur.execute(
                "insert into rsk_act_item (act_id, violation_id) values (%s,%s) on conflict do nothing",
                (act_id, row["id"]),
            )

    # rsk_act_item для основного (разобранного) акта 4183-159 — is_repeat уже
    # перенесён в rsk_violation.is_repeat при парсинге; здесь просто
    # фиксируем сам факт вхождения в этот акт для всех, кто сейчас open/
    # rejected/partially_closed (закрытые до даты акта в него не входят,
    # см. сверку в parse_rsk.py).
    cur.execute(
        "select sys_no, id from rsk_violation where state in ('open','rejected','partially_closed')"
    )
    for r in cur.fetchall():
        cur.execute(
            "insert into rsk_act_item (act_id, violation_id) values (%s,%s) on conflict do nothing",
            (main_act_id, r["id"]),
        )

    # ---- rsk_import_issue — пересобираем на каждый запуск ----
    cur.execute("delete from rsk_import_issue")
    for issue in data["issues"]:
        cur.execute(
            "insert into rsk_import_issue (sys_no, severity, kind, message, raw_value) values (%s,%s,%s,%s,%s)",
            (issue["sys_no"], issue["severity"], issue["kind"], issue["message"], issue["raw_value"]),
        )

    conn.commit()

    print(f"rsk_violation: {n_inserted} новых, {n_updated} обновлено")
    print(f"rsk_import_issue: {len(data['issues'])} строк")

    cur.execute("select count(*) as n from rsk_violation")
    print("Итого rsk_violation:", cur.fetchone()["n"])
    cur.execute("select state, count(*) as n from rsk_violation group by state order by n desc")
    for r in cur.fetchall():
        print(" ", r["state"], r["n"])

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
