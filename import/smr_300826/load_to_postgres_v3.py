#!/usr/bin/env python3
"""
Загрузка JSON, полученного parse_excel.py, в PostgreSQL (схема migrations/001_init.sql).

Не запускался ни разу до появления доступа к БД tm35 — см.
docs/DB_VERIFICATION.md за статусом. Перед первым запуском: применить
migrations/001_init.sql, задать переменную окружения TM35_DSN (например
"postgresql://user:pass@host:5432/tm35"), затем прогнать на выгрузке из
import/output/.

Не пытается угадывать/нормализовывать данные — data_quality_flag и
data_quality_note переносятся из JSON как есть (их проставляет
parse_excel.py). Записи, для которых парсер не смог построить валидную
дату (bad_calendar_date), в daily_progress не попадают в принципе — они
уходят в import_unresolved_cell отдельным шагом.
"""
import json
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# ВЕРСИЯ v3 (29.08.2026, блокер к запуску 01.09) — правило разбора
# ячеек календарной матрицы «СМР общий ГПР», выписано и проверено
# ДО применения (перепись по всей матрице: 855 чисел, 252 "ПОДРЯД",
# 93 "N чел.", 1 пустая строка-шум — других видов значений нет):
#
#   1. Число (int/float)              -> число людей как есть.
#   2. "N чел"/"N чел." (регистр любой) -> извлечь N, то же число людей.
#   3. "ПОДРЯД"/"Подряд"              -> НЕ число людей. Работу в этот
#      день ведёт субподряд, численность неизвестна. planned_crew/
#      actual_crew остаётся NULL (не 0, не выдуманное число), сырой
#      текст "ПОДРЯД" сохраняется в planned_crew_raw/actual_crew_raw
#      (тот же принцип, что fact_pct/fact_pct_raw — миграция 019).
#   4. "В"/"в" в колонках без заголовка План/Факт (25/24 воскресных
#      "безголовых" колонки) — это разметка календаря (выходной), не
#      относится ни к одной работе. Такие колонки parse_excel.py в
#      calendar_cols вообще не попадают (build_calendar_columns требует
#      числовой день в строке 3) — до этого загрузчика такие ячейки не
#      доходят физически, отдельно обрабатывать здесь нечего.
#   5. Пусто -> нет записи.
#   6. Что-то ещё, не подходящее под 1-5 -> НЕ угадывать: crew=None,
#      raw=сырой текст, data_quality_flag='needs_review' с пометкой
#      исходной ячейки — по факту в реальных данных таких случаев 0
#      (кроме одной пустой строки, которая уже покрыта правилом 5).
_NCHEL_RE = re.compile(r"^(\d+)\s*чел\.?$", re.IGNORECASE)


def parse_crew_cell(val):
    """Возвращает (crew:int|None, raw:str|None, needs_review:bool)."""
    if val is None:
        return None, None, False
    if isinstance(val, (int, float)):
        return int(val), None, False
    s = str(val).strip()
    if not s:
        return None, None, False
    if s.lower() in ("подряд",):
        return None, s, False
    m = _NCHEL_RE.match(s)
    if m:
        return int(m.group(1)), None, False
    # Не число, не "ПОДРЯД", не "N чел." — не встречалось в проверенных
    # данных, но на случай нового файла не угадываем молча.
    return None, s, True


def load_works(cur, works):
    rows = [
        (
            w["code"], w["source"], w.get("section"), w["name"], w.get("location"),
            w.get("unit"), w.get("volume"), w.get("volume_raw"), w.get("executor_type"),
            w.get("status", "not_started"), w.get("comment"),
            w.get("source_row_ref"), w.get("fact_pct"), w.get("fact_pct_raw"),
            w.get("data_quality_flag", "ok"), w.get("data_quality_note"),
        )
        for w in works
    ]
    execute_values(
        cur,
        """
        insert into work (code, source, section, name, location, unit, volume, volume_raw,
                           executor_type, status, comment, source_row_ref,
                           fact_pct, fact_pct_raw, data_quality_flag, data_quality_note)
        values %s
        on conflict (code) do update set
            section = excluded.section,
            name = excluded.name,
            location = excluded.location,
            unit = excluded.unit,
            volume = excluded.volume,
            volume_raw = excluded.volume_raw,
            executor_type = excluded.executor_type,
            status = excluded.status,
            comment = excluded.comment,
            fact_pct = excluded.fact_pct,
            fact_pct_raw = excluded.fact_pct_raw,
            data_quality_flag = excluded.data_quality_flag,
            data_quality_note = excluded.data_quality_note,
            updated_at = now()
        """,
        rows,
    )
    return len(rows)


def load_daily_progress(cur, daily):
    """
    daily-записи из parse_excel.py — по одной на (работа, дата, plan|fact),
    т.е. для одного дня обычно две записи (kind=plan и kind=fact), которые
    вместе образуют одну строку daily_progress (planned_crew, actual_crew).
    Схема daily_progress хранит план и факт в одной строке — сворачиваем
    пары по (work_code, date) перед вставкой.
    """
    merged = {}
    n_podryad = 0
    n_unrecognized = 0
    for d in daily:
        key = (d["work_code"], d["date"])
        rec = merged.setdefault(key, {
            "planned_crew": None, "actual_crew": None,
            "planned_crew_raw": None, "actual_crew_raw": None,
            "comment": None, "data_quality_flag": "ok", "data_quality_note": None,
        })
        crew, raw, needs_review = parse_crew_cell(d["value"])
        if raw == "ПОДРЯД" or (raw and raw.lower() == "подряд"):
            n_podryad += 1
        if needs_review:
            n_unrecognized += 1
        if d["kind"] == "plan":
            rec["planned_crew"] = crew
            rec["planned_crew_raw"] = raw
        else:
            rec["actual_crew"] = crew
            rec["actual_crew_raw"] = raw
        if d.get("comment"):
            rec["comment"] = ((rec["comment"] + "; ") if rec["comment"] else "") + d["comment"]
        if d.get("data_quality_flag") == "needs_review" or needs_review:
            rec["data_quality_flag"] = "needs_review"
            rec["data_quality_note"] = d.get("data_quality_note") or (
                f"Нераспознанное значение ячейки: {raw!r}" if needs_review else rec["data_quality_note"]
            )

    cur.execute("select code, id from work")
    work_id_by_code = dict(cur.fetchall())

    rows = []
    skipped_unknown_work = 0
    for (work_code, date), rec in merged.items():
        work_id = work_id_by_code.get(work_code)
        if work_id is None:
            skipped_unknown_work += 1
            continue
        rows.append((
            date, work_id, rec["planned_crew"], rec["actual_crew"],
            rec["planned_crew_raw"], rec["actual_crew_raw"], rec["comment"],
            "excel_import", rec["data_quality_flag"], rec["data_quality_note"],
        ))

    execute_values(
        cur,
        """
        insert into daily_progress (date, work_id, planned_crew, actual_crew,
                                     planned_crew_raw, actual_crew_raw, comment,
                                     source, data_quality_flag, data_quality_note)
        values %s
        on conflict (date, work_id, source) do update set
            planned_crew = excluded.planned_crew,
            actual_crew = excluded.actual_crew,
            planned_crew_raw = excluded.planned_crew_raw,
            actual_crew_raw = excluded.actual_crew_raw,
            comment = excluded.comment,
            data_quality_flag = excluded.data_quality_flag,
            data_quality_note = excluded.data_quality_note,
            updated_at = now()
        """,
        rows,
    )
    print(f"  из них 'ПОДРЯД' (число людей неизвестно, сохранено как признак): {n_podryad}")
    if n_unrecognized:
        print(f"  ВНИМАНИЕ: нераспознанных значений (не число/ПОДРЯД/N чел.): {n_unrecognized}")
    return len(rows), skipped_unknown_work, len(merged)


def load_unresolved_cells(cur, quality_issues):
    # Идемпотентность: у таблицы нет уникального ключа на (sheet, cell_ref) —
    # без явной очистки перед вставкой повторный прогон дублирует строки.
    cur.execute("delete from import_unresolved_cell where issue_type = 'bad_calendar_date'")
    rows = [
        (
            i["sheet"], i["cell"], i.get("work_code"), i["type"],
            json.dumps(i, ensure_ascii=False),
        )
        for i in quality_issues
        if i["type"] == "bad_calendar_date"
    ]
    if not rows:
        return 0
    execute_values(
        cur,
        """
        insert into import_unresolved_cell (sheet, cell_ref, work_code, issue_type, raw_payload)
        values %s
        """,
        [(s, c, wc, t, p) for s, c, wc, t, p in rows],
    )
    return len(rows)


BASELINE_COMMENTS = {
    "matrix_schedule": (
        "Baseline восстановлен из календарной матрицы Excel (первый/последний "
        "день с планом > 0) — по итогам диагностики (docs/MATRIX_DIAGNOSTICS.md) "
        "матрица ведёт себя как график работ, не ресурсная разнарядка. "
        "Не подтверждён координатором/заказчиком формально."
    ),
    "text_month_only": (
        "Baseline выведен только из текстового поля \"Месяц выполнения работ\" "
        "(месячная точность, единственный источник) — календарных данных по "
        "этой работе нет. Ниже уверенности, чем matrix_schedule."
    ),
    "no_data": "Baseline не восстановлен ни из одного источника.",
}


def load_baseline(cur, works):
    # baseline_schedule целиком выводится этим загрузчиком (не редактируется
    # через UI) — безопасно и проще очищать всю таблицу перед вставкой,
    # чем подбирать фильтр по тексту комментария (менялся между версиями).
    cur.execute("delete from baseline_schedule")
    rows = [
        (
            w["code"], w.get("baseline_start"), w.get("baseline_finish"), w.get("baseline_crew"),
            w.get("baseline_source", "no_data"), w.get("baseline_confidence", "none"),
            BASELINE_COMMENTS[w.get("baseline_source", "no_data")],
        )
        for w in works
    ]
    execute_values(
        cur,
        """
        insert into baseline_schedule
            (work_id, plan_start, plan_finish, plan_crew, baseline_source, confidence, comment)
        select w.id, v.plan_start::date, v.plan_finish::date, v.plan_crew::int,
               v.baseline_source::baseline_source, v.confidence::baseline_confidence, v.comment
        from (values %s) as v(code, plan_start, plan_finish, plan_crew, baseline_source, confidence, comment)
        join work w on w.code = v.code
        """,
        rows,
    )
    return len(rows)


DAY_BLOCKER_MARKER = "[день-уровень, Excel]: "


def load_day_blockers(cur, day_comments):
    # Идемпотентность: удаляем ранее загруженные день-уровневые блокеры
    # (по маркеру в description), затем вставляем заново — иначе повторный
    # прогон дублирует одни и те же комментарии при каждом импорте.
    cur.execute(
        "delete from blocker where description like %s",
        (DAY_BLOCKER_MARKER + "%",),
    )
    if not day_comments:
        return 0
    rows = [
        (
            c["blocker_type"],
            DAY_BLOCKER_MARKER + c["comment"],
            c["date"],
        )
        for c in day_comments
    ]
    execute_values(
        cur,
        """
        insert into blocker (blocker_type, description, created_at)
        values %s
        """,
        [(bt, desc, f"{d} 00:00:00+00") for bt, desc, d in rows],
    )
    return len(rows)


def main(dsn, out_dir):
    out_dir = Path(out_dir)
    works = json.loads((out_dir / "works.json").read_text(encoding="utf-8"))
    daily = json.loads((out_dir / "daily_progress.json").read_text(encoding="utf-8"))
    quality = json.loads((out_dir / "quality_report.json").read_text(encoding="utf-8"))
    day_comments_path = out_dir / "day_level_comments.json"
    day_comments = json.loads(day_comments_path.read_text(encoding="utf-8")) if day_comments_path.exists() else []

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            n_works = load_works(cur, works)
            n_daily, skipped, merged_total = load_daily_progress(cur, daily)
            n_unresolved = load_unresolved_cells(cur, quality)
            n_day_blockers = load_day_blockers(cur, day_comments)
            n_baseline = load_baseline(cur, works)
        print(f"work: загружено/обновлено {n_works} (ожидалось {len(works)})")
        print(f"daily_progress: строк после свёртки план+факт по (работа,дата) — {merged_total}, "
              f"загружено {n_daily}, пропущено (work_id не найден) {skipped}")
        print(f"import_unresolved_cell: {n_unresolved}")
        print(f"blocker (день-уровневые из Excel): {n_day_blockers}")
        print(f"baseline_schedule (временный, из «Месяц выполнения работ»): {n_baseline}")
    finally:
        conn.close()


if __name__ == "__main__":
    dsn = os.environ.get("TM35_DSN")
    if not dsn:
        print("Задайте TM35_DSN — загрузка не выполнена.", file=sys.stderr)
        sys.exit(1)
    out = sys.argv[1] if len(sys.argv) > 1 else "/home/oleg/Documents/TM-35/import/output"
    main(dsn, out)
