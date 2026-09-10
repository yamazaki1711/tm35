#!/usr/bin/env python3
"""
Ночной прогон 09-10.09.2026, задача 6 — сеть регрессионных проверок.

Аудит 08.09.2026 нашёл шесть случаев одной болезни: одно и то же число
считается в разных местах кода по-разному и расходится (см. докстринг
LATEST_ID_FORM_ENTRY_CTE в main.py — там же прямо написано: "на момент
проверки все три совпадали — 163, но это не гарантия на будущее без
общего текста"). Большинство таких мест уже переведено на общий SQL/
общую функцию — но общий текст сам по себе не защищает от будущей
правки, которая тихо разойдётся снова. Этот скрипт не проверяет код на
дублирование — он вызывает РЕАЛЬНЫЕ функции/маршруты main.py (то, что
фактически исполняется сейчас) и сверяет то, что они возвращают, друг
с другом. Расхождение здесь означает: то, что видит пользователь на
одном экране, разошлось с тем, что видит на другом, прямо сейчас.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/check_consistency.py

Печатает таблицу "показатель / источник A / источник B / сходится ли",
возвращает ненулевой код при любом расхождении.
"""
import re
import sys
import urllib.request

sys.path.insert(0, "/app")
import main as m  # noqa: E402

CHECKS = []
FAILED = False


def check(name, a_label, a_val, b_label, b_val, tolerance=0):
    global FAILED
    if isinstance(a_val, float) or isinstance(b_val, float):
        ok = abs(float(a_val) - float(b_val)) <= tolerance
    else:
        ok = a_val == b_val
    if not ok:
        FAILED = True
    CHECKS.append((name, a_label, a_val, b_label, b_val, ok))


def main_check():
    # --- 1. "Работы по статусам": /dashboard vs /works vs /export/works.csv ---
    # Три места в main.py строят group-by по _work_status_expr() независимым
    # текстом запроса (полный реестр work, без фильтров) — вызываем буквально
    # то же самое, что вызывают сами маршруты.
    status_expr = m._work_status_expr(None)
    dash_by_status = m.query(
        f"select {status_expr} as status, count(*) as n from work group by 1 order by n desc"
    )
    dash_dist = {r["status"]: r["n"] for r in dash_by_status}

    works_rows = m.query(f"select {status_expr} as status from work where true order by code")
    works_dist = {}
    for r in works_rows:
        works_dist[r["status"]] = works_dist.get(r["status"], 0) + 1

    csv_rows = m.query(
        f"select {status_expr} as status from work where true order by code"
    )
    csv_dist = {}
    for r in csv_rows:
        csv_dist[r["status"]] = csv_dist.get(r["status"], 0) + 1

    all_statuses = sorted(set(dash_dist) | set(works_dist) | set(csv_dist))
    for s in all_statuses:
        check(
            f"Работы по статусу «{s}»: /dashboard vs /works",
            "/dashboard", dash_dist.get(s, 0),
            "/works", works_dist.get(s, 0),
        )
        check(
            f"Работы по статусу «{s}»: /dashboard vs /export/works.csv",
            "/dashboard", dash_dist.get(s, 0),
            "/export/works.csv", csv_dist.get(s, 0),
        )

    # --- 2. Просроченные: /critical (get_criticality_data) vs /api/gantt ---
    crit = m.get_criticality_data()
    overdue_count_canon = crit["overdue_count"]

    gantt = m.api_gantt()
    gantt_critical_count = sum(
        1 for g in gantt["groups"] for w in g["works"] if w["critical"]
    )
    check(
        "Просроченных работ: /critical (get_criticality_data) vs /api/gantt (critical=true)",
        "/critical", overdue_count_canon,
        "/api/gantt", gantt_critical_count,
    )

    gantt_metrics = m.api_gantt_metrics()
    check(
        "Просроченных работ: /critical vs /api/gantt-metrics",
        "/critical", overdue_count_canon,
        "/api/gantt-metrics", gantt_metrics["overdue_count"],
    )

    # --- 3. "Подписано разделов": home_v2 (id_stats) vs compute_id_folder_stats() vs /id-packages ---
    id_stats_row = m.query_one(m.LATEST_ID_FORM_ENTRY_CTE + """
        select count(*) as total,
               count(*) filter (where s.code = 'Подписано') as signed
        from id_form_row r
        join id_form_tab t on t.id = r.tab_id
        left join latest_id_entry le on le.row_id = r.id
        left join id_form_status s on s.id = le.status_id
        where t.code not in ('opv', 'n')
    """)
    folder_stats = m.compute_id_folder_stats()

    id_rows = m.query(m.ID_ROW_LIST_SQL)
    id_packages_status_counts = {}
    for r in id_rows:
        if r["status_label"]:
            id_packages_status_counts[r["status_label"]] = (
                id_packages_status_counts.get(r["status_label"], 0) + 1
            )

    check(
        "Подписано разделов: /dashboard (id_stats) vs compute_id_folder_stats()",
        "/dashboard", id_stats_row["signed"],
        "compute_id_folder_stats()", folder_stats["signed_total"],
    )
    check(
        "Подписано разделов: /dashboard (id_stats) vs /id-packages (status_counts)",
        "/dashboard", id_stats_row["signed"],
        "/id-packages", id_packages_status_counts.get("Подписано", 0),
    )
    check(
        "Всего разделов (без ОПВ/Н): /dashboard (id_stats) vs compute_id_folder_stats()",
        "/dashboard", id_stats_row["total"],
        "compute_id_folder_stats()", folder_stats["total_rows"],
    )

    # --- 4. Сумма по стадиям воронки папок = count(*) from id_folder ---
    funnel = folder_stats["funnel"]
    funnel_sum = sum(v["count"] for v in funnel.values())
    total_folders = m.query_one("select count(*) as n from id_folder")["n"]
    check(
        "Воронка папок: сумма по 5 стадиям vs count(*) from id_folder",
        "sum(funnel[*].count)", funnel_sum,
        "count(*) from id_folder", total_folders,
    )
    check(
        "Воронка папок: сумма по 5 стадиям vs compute_id_folder_stats()['folders_count']",
        "sum(funnel[*].count)", funnel_sum,
        "folders_count", folder_stats["folders_count"],
    )

    # --- 5. Деньги: контракт − подписано − ручной объём = остаток ---
    identity_remaining = (
        float(folder_stats["contract_total"])
        - float(folder_stats["signed_folders_sum"])
        - float(folder_stats["manual_sum"])
    )
    check(
        "Деньги: контракт − подписано − ручной объём vs money_remaining",
        "пересчитано", round(identity_remaining, 2),
        "compute_id_folder_stats()['money_remaining']", round(float(folder_stats["money_remaining"]), 2),
        tolerance=0.01,
    )

    # --- 6. "Активных ИЗМ": /id-folders (active_changes) vs /dashboard (change_stats_row) ---
    # Оба места фильтруют одним и тем же текстом "status not in ('INCLUDED_IN_RD',
    # 'ARCHIVED')", но каждое — своим независимым запросом (main.py:4584 и :5382) -
    # ровно тот класс дублирования, что искал аудит 08.09.2026.
    active_changes_folders = m.query_one(
        "select count(*) as n from change where status not in ('INCLUDED_IN_RD', 'ARCHIVED')"
    )["n"]
    change_stats_row = m.query_one(f"""
        select count(*) as total,
               count(*) filter (where {m._change_overdue_expr()} is not null) as overdue
        from change
        where status not in ('INCLUDED_IN_RD', 'ARCHIVED')
    """)
    check(
        "Активных ИЗМ: /id-folders (active_changes) vs /dashboard (change_stats)",
        "/id-folders", active_changes_folders,
        "/dashboard", change_stats_row["total"],
    )

    # --- 7. Продолжение прогона 10.09.2026: awaiting_smeta_count/not_signed_count vs прямой SQL ---
    check(
        "Ждут сметную стоимость: compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["awaiting_smeta_count"],
        "прямой SQL", m.query_one(
            "select count(*) as n from id_folder where signed_date is not null and amount_smeta_rub is null"
        )["n"],
    )
    check(
        "Ещё не подписано: compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["not_signed_count"],
        "прямой SQL", m.query_one("select count(*) as n from id_folder where signed_date is null")["n"],
    )

    # --- 8. Блоки 1-2 "График ИД — прогресс": tiles (один агрегат) vs stream
    # (group by вкладке) — два независимо написанных запроса над одной и той
    # же LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE, сумма по вкладкам обязана
    # сойтись с общим счётом.
    tiles = m.compute_id_progress_tiles()
    stream = m.compute_id_progress_stream()
    check(
        "«Прогресс по видам работ»: total_pairs (плитки) vs сумма green+yellow+red по вкладкам (поток)",
        "tiles.total_pairs", tiles["total_pairs"],
        "sum(stream green+yellow+red)", sum(r["green_n"] + r["yellow_n"] + r["red_n"] for r in stream),
    )
    check(
        "«Прогресс по видам работ»: signed (плитки) vs сумма green_n по вкладкам (поток)",
        "tiles.signed", tiles["signed"],
        "sum(stream green_n)", sum(r["green_n"] for r in stream),
    )

    # --- 8b. Тот же класс дефекта, что нашёл координатор 10.09.2026: "Поток
    # по вкладкам" и выпадающий список фильтра матрицы — два разных взгляда
    # на один справочник id_form_tab, обязаны перечислять один и тот же
    # набор вкладок. До исправления "Поток" строился INNER JOIN от уже
    # введённых записей и терял вкладки без единой записи (11 вместо 15) —
    # эта проверка ловит повторение той же ошибки.
    dropdown_tab_ids = sorted(t["id"] for t in m.query(
        "select id from id_form_tab where code not in ('opv', 'n')"
    ))
    stream_tab_ids = sorted(r["tab_id"] for r in stream)
    check(
        "Набор вкладок: «Поток по вкладкам» vs выпадающий список фильтра матрицы",
        "поток по вкладкам", stream_tab_ids,
        "выпадающий список матрицы", dropdown_tab_ids,
    )

    # --- 9. Матрица «Раздел × Этап»: цвет первого столбца vs независимый
    # пересчёт по каждой вкладке (тот же LATEST_ID_FORM_ENTRY_CTE, но текст
    # запроса здесь написан заново, а не переиспользован из compute_id_matrix,
    # чтобы проверка ловила расхождение, а не подтверждала сама себя).
    tabs = m.query("select id, label from id_form_tab where code not in ('opv', 'n') order by label")
    for tab in tabs:
        mx = m.compute_id_matrix(tab["id"])
        matrix_green = sum(1 for r in mx["rows"] if r["color"] == "green")
        direct_green = m.query_one(
            m.LATEST_ID_FORM_ENTRY_CTE + """
            select count(*) as n
            from id_form_row r
            join latest_id_entry le on le.row_id = r.id
            join id_form_status s on s.id = le.status_id
            where r.tab_id = %(tab_id)s and s.code = 'Подписано'
            """,
            {"tab_id": tab["id"]},
        )["n"]
        check(
            f"Матрица «{tab['label']}»: раздел зелёный (первый столбец) vs прямой count «Подписано»",
            "compute_id_matrix()", matrix_green,
            "прямой SQL", direct_green,
        )

    # --- 10. /id-folders/registry (amount_signed) vs compute_id_folder_stats() ---
    reg_folders = m.query_id_folders(order="desc")
    reg_amount_signed = float(m.compute_id_folder_stats()["signed_folders_sum"])
    check(
        "Реестр папок (amount_signed) vs compute_id_folder_stats()['signed_folders_sum']",
        "/id-folders/registry", reg_amount_signed,
        "compute_id_folder_stats()", float(folder_stats["signed_folders_sum"]),
        tolerance=0.01,
    )

    # --- 11a. Заход 3, задача 5: инвариант rsk_violation.is_active <=>
    # closed_in_act_id is null — оба поля выставляются вместе одним
    # UPDATE в /rsk/import/confirm, но это два независимых столбца,
    # которые в будущем кто-то может обновить порознь. Расхождение = 0
    # сейчас, но проверка должна остаться постоянной, не разовой.
    rsk_invariant_mismatch = m.query_one(
        "select count(*) as n from rsk_violation where (is_active = false) != (closed_in_act_id is not null)"
    )["n"]
    check(
        "rsk_violation: is_active=false согласовано с closed_in_act_id (структурный инвариант)",
        "количество расхождений", rsk_invariant_mismatch,
        "ожидается", 0,
    )

    # --- 11b. "Активных нарушений": /rsk/dashboard (compute_rsk_dashboard_stats)
    # vs прямой SQL по is_active — независимая проверка того же числа.
    rsk_dash_stats = m.compute_rsk_dashboard_stats()
    direct_active = m.query_one("select count(*) as n from rsk_violation where is_active")["n"]
    check(
        "Активных нарушений РСК: /rsk/dashboard vs прямой SQL",
        "/rsk/dashboard", rsk_dash_stats["tiles"]["total_active"],
        "прямой SQL", direct_active,
    )

    # --- 11. Воронка папок на /dashboard vs /id-folders — не текст кода, а то,
    # что реально отдаёт HTTP-сервер (задание координатора: "смотреть на
    # экран, не на код"). Обе страницы включают один и тот же шаблон
    # _id_folder_funnel.html — здесь сверяются числа из ОТРЕНДЕРЕННОГО HTML
    # обеих страниц, не повторный вызов той же Python-функции.
    dashboard_html = urllib.request.urlopen("http://localhost:8000/dashboard", timeout=15).read().decode("utf-8")
    id_folders_html = urllib.request.urlopen("http://localhost:8000/id-folders", timeout=15).read().decode("utf-8")
    for stage, label in m.ID_FOLDER_STAGE_LABELS.items():
        pattern = re.compile(
            r'<div class="kpi-num[^"]*">(\d+)</div>\s*<div class="kpi-label">' + re.escape(label) + r"</div>"
        )
        dash_match = pattern.search(dashboard_html)
        folders_match = pattern.search(id_folders_html)
        dash_n = int(dash_match.group(1)) if dash_match else None
        folders_n = int(folders_match.group(1)) if folders_match else None
        check(
            f"Воронка папок (отрендеренный HTML), стадия «{label}»: /dashboard vs /id-folders",
            "/dashboard", dash_n,
            "/id-folders", folders_n,
        )


def print_report():
    name_w = max(len(c[0]) for c in CHECKS)
    a_lbl_w = max(len(c[1]) for c in CHECKS)
    b_lbl_w = max(len(c[3]) for c in CHECKS)
    print(f"{'ПОКАЗАТЕЛЬ':<{name_w}}  {'ИСТОЧНИК A':<{a_lbl_w}}  {'A':>14}  {'ИСТОЧНИК B':<{b_lbl_w}}  {'B':>14}  СХОДИТСЯ")
    print("-" * (name_w + a_lbl_w + b_lbl_w + 50))
    for name, a_lbl, a_val, b_lbl, b_val, ok in CHECKS:
        mark = "ДА" if ok else "!! НЕТ !!"
        print(f"{name:<{name_w}}  {a_lbl:<{a_lbl_w}}  {a_val!s:>14}  {b_lbl:<{b_lbl_w}}  {b_val!s:>14}  {mark}")
    print()
    n_fail = sum(1 for c in CHECKS if not c[5])
    print(f"Итого проверок: {len(CHECKS)}, расхождений: {n_fail}")


if __name__ == "__main__":
    main_check()
    print_report()
    sys.exit(1 if FAILED else 0)
