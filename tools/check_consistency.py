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
import json
import re
import sys
import urllib.request

sys.path.insert(0, "/app")
import main as m  # noqa: E402
import db  # noqa: E402 — get_conn() для живой UPDATE+ROLLBACK проверки триггеров, main.py его не реэкспортирует

CHECKS = []
FAILED = False


def _parse_ru_money(s):
    """Обратный разбор ru_money-отформатированного числа с экрана
    («4 078 191 380,98») в float — для проверок §7 ТЗ 15.09.2026, где
    тождество сверяется по тому, что реально нарисовано на странице, а
    не повторным вызовом той же функции."""
    if s is None:
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


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

    # --- 5. Деньги: контракт − подписано по КС-3 − ручной объём = остаток
    # (ТЗ Якименко А.И. №10, 23.09.2026, п.3 — «Подписано ИД» больше не
    # вычитается, КС-3 и «Подписано ИД» одни и те же деньги с двух сторон;
    # см. compute_id_folder_stats()).
    identity_remaining = (
        float(folder_stats["contract_total"])
        - float(folder_stats["ks3_sum"])
        - float(folder_stats["manual_sum"])
    )
    check(
        "Деньги: контракт − подписано по КС-3 − ручной объём vs money_remaining",
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

    # --- 7. ТЗ Якименко А.И. №10, 23.09.2026 — «Подписано ИД» (бывшая
    # «Подписано по КС-2», п.4: та же величина, только имя переименовано)
    # — сверка count() против прямого SQL; и «Подписано по КС-3» (п.5,
    # новый сектор id_ks3_entry) — сверка суммы против прямого SQL.
    check(
        "Подписано ИД (папок с ks2_date): compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["ks2_count"],
        "прямой SQL", m.query_one(
            "select count(*) as n from id_folder where ks2_date is not null"
        )["n"],
    )
    check(
        "Подписано по КС-3: compute_id_folder_stats()['ks3_sum'] vs прямой SQL sum(id_ks3_entry)",
        "compute_id_folder_stats()", round(float(folder_stats["ks3_sum"]), 2),
        "прямой SQL", round(float(m.query_one("select coalesce(sum(amount_rub),0) as s from id_ks3_entry")["s"]), 2),
        tolerance=0.01,
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

    # --- 9. Матрица «Раздел × Этап»: цвет замороженного столбца (текущий
    # статус раздела) vs независимый пересчёт по каждой вкладке (тот же
    # LATEST_ID_FORM_ENTRY_CTE, но текст запроса здесь написан заново, а
    # не переиспользован из compute_id_matrix, чтобы проверка ловила
    # расхождение, а не подтверждала сама себя).
    tabs = m.query("select id, label from id_form_tab where code not in ('opv', 'n') order by label")
    for tab in tabs:
        mx = m.compute_id_matrix(tab["id"])
        matrix_green = sum(1 for r in mx["rows"] if r["current_color"] == "green")
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
            f"Матрица «{tab['label']}»: раздел зелёный (замороженный столбец) vs прямой count «Подписано»",
            "compute_id_matrix()", matrix_green,
            "прямой SQL", direct_green,
        )

    # --- 9b. Заход 6, задача 3, 11.09.2026 — обязательная по заданию сверка:
    # числа в сетке /id-progress должны сходиться с плитками той же вкладки
    # выше на странице. Берём последнюю (самую свежую) неделю ПОЛНОГО периода
    # (show_all=True, а не окно по умолчанию — окно может не доходить до
    # сегодняшнего дня при навигации, а тут нужен именно самый актуальный
    # снимок) — "сколько разделов сейчас зелёные" по матрице обязано
    # совпасть с тем же счётом по LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE,
    # которым же независимо (см. compute_id_progress_stream) считается
    # плитка "Поток по вкладкам" green_n. Обе величины — количество
    # ПОДПИСАННЫХ связок раздел+вид работы на вкладке; путь к числу разный
    # (одна взята из мгновенного count(*) по CTE, другая — из
    # покомпонентной as-of-сегодня реконструкции истории), но ответ обязан
    # быть один и тот же, иначе одна из двух реализаций считает не то же
    # самое "сейчас".
    stream_by_tab = {r["tab_id"]: r for r in stream}
    for tab in tabs:
        mx_full = m.compute_id_matrix(tab["id"], show_all=True)
        # signed берём из ПОСЛЕДНЕЙ недели каждой строки по позиции в списке
        # (последняя неделя полного периода — самая свежая доступная точка).
        matrix_signed_now = sum(r["cells"][-1]["signed"] for r in mx_full["rows"] if not r["cells"][-1]["empty"])
        tile_green = stream_by_tab.get(tab["id"], {}).get("green_n", 0)
        check(
            f"Матрица «{tab['label']}» (последняя неделя, всего подписано) vs плитка «Поток по вкладкам» green_n",
            "compute_id_matrix(show_all=True), последняя неделя", matrix_signed_now,
            "compute_id_progress_stream()", tile_green,
        )

    # --- 10. Единая «Стоимость, ₽» (координатор, 16.09.2026, ТЗ Якименко) —
    # сумма id_folder_cost() по списку /id-folders (столбец «Стоимость, ₽»)
    # обязана совпасть с суммой того же выражения, посчитанной прямым SQL:
    # один coalesce(amount_smeta_rub, amount_rub), не два разных пути к числу.
    reg_folders = m.query_id_folders(order="desc")
    list_cost_sum = round(sum(m.id_folder_cost(f) for f in reg_folders), 2)
    direct_cost_sum = round(float(m.query_one(
        "select coalesce(sum(coalesce(amount_smeta_rub, amount_rub)), 0) as s from id_folder"
    )["s"]), 2)
    check(
        "Стоимость, ₽: сумма id_folder_cost() по списку /id-folders vs прямой SQL coalesce()",
        "/id-folders (id_folder_cost)", list_cost_sum,
        "прямой SQL coalesce(amount_smeta_rub, amount_rub)", direct_cost_sum,
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

    # --- 11c. ТЗ Якименко А.И., 16.09.2026, §6 — "Корректировка ПД/РД"
    # больше не определяется одним track_phys='fact' (значение убрано из
    # выбора в «Физика»), а треком «Проект» — плюс легаси-строки, у
    # которых track_phys='fact' ещё сохранился. Прямой SQL — та же логика,
    # переписанная заново, чтобы проверка не подтверждала сама себя.
    direct_needs_rd = m.query_one(f"""
        select count(*) as n
        from rsk_violation v left join rsk_processing p on p.violation_id = v.id
        where v.is_active and (coalesce(p.track_design,'unknown') = 'not_done'
            or coalesce(p.track_phys,'unknown') = 'fact')
    """)["n"]
    check(
        "«Корректировка ПД/РД» (needs_rd): /rsk/dashboard vs прямой SQL",
        "/rsk/dashboard", rsk_dash_stats["tiles"]["needs_rd"],
        "прямой SQL", direct_needs_rd,
    )

    # --- 11d. ТЗ Якименко А.И., 16.09.2026, §3 — «Устранено» (слой 2,
    # rsk_processing.resolved) не должно совпадать по смыслу со
    # структурным «Снято» (rsk_violation.is_active=false) — независимая
    # прямая проверка тайла и явная проверка, что оба состояния умеют
    # расходиться (устранено=true у ещё активного нарушения — не ошибка).
    direct_resolved = m.query_one(
        "select count(*) as n from rsk_violation v join rsk_processing p on p.violation_id = v.id "
        "where v.is_active and p.resolved"
    )["n"]
    check(
        "«Устранено» (resolved): /rsk/dashboard vs прямой SQL",
        "/rsk/dashboard", rsk_dash_stats["tiles"]["resolved"],
        "прямой SQL", direct_resolved,
    )

    # --- 11. Труба папок ИД — ТЗ №10, 23.09.2026, п.2 убрал трубу с
    # /dashboard (заменена «Потоком по вкладкам», см. проверку ниже),
    # осталась только на /id-folders — сравнивать больше не с чем на
    # уровне отрендеренного HTML между двумя страницами (внутренняя
    # сходимость трубы — сумма по 5 стадиям vs count(*) — уже покрыта
    # проверкой №4 выше через compute_id_folder_funnel(), той же функции,
    # что рисует единственную оставшуюся трубу).
    dashboard_html = urllib.request.urlopen("http://localhost:8000/dashboard", timeout=15).read().decode("utf-8")
    id_folders_html = urllib.request.urlopen("http://localhost:8000/id-folders", timeout=15).read().decode("utf-8")
    id_progress_html = urllib.request.urlopen("http://localhost:8000/id-progress", timeout=15).read().decode("utf-8")

    # --- 12. Заход 4, задача 1: ни один id_form_row не должен состоять в
    # двух группах «Графика ИД» сразу. Уже гарантировано ограничением
    # unique(row_id) в id_report_group_row (миграция 030) — вставка
    # дубля физически невозможна — но проверка остаётся постоянной, не
    # разовой: если кто-то когда-нибудь ослабит ограничение в схеме, эта
    # строка должна поймать нарушение раньше, чем оно попадёт в отчёт.
    duplicate_row_ids = m.query(
        "select row_id, count(*) as n from id_report_group_row group by row_id having count(*) > 1"
    )
    check(
        "id_report_group_row: ни один раздел не состоит в двух группах сразу",
        "разделов с >1 группой", len(duplicate_row_ids),
        "ожидается", 0,
    )

    # --- 13. Заход 6, задача 2: номера папок ИД — гапless ТМ-001..N в
    # порядке создания, без дублей. Не гарантировано схемой (name без
    # unique) — держим проверкой, раз рассчитываем на этот порядок при
    # выдаче следующего номера (api_id_folder_create).
    id_folders_ordered = m.query("select id, name, created_at from id_folder order by created_at, id")
    expected_names = [f"ТМ-{i:03d}" for i in range(1, len(id_folders_ordered) + 1)]
    actual_names = [f["name"] for f in id_folders_ordered]
    check(
        "Номера папок ИД: ТМ-001..N без дыр и дублей, строго по порядку создания",
        "фактический порядок", actual_names,
        "ожидаемый порядок", expected_names,
    )

    # --- 15. Заход 8, 14.09.2026 — развод "Обзор — Заказчику, разделы —
    # ПТО" (см. home.html): Обзор больше не единственное место с этими
    # цифрами, каждая обязана буквально совпасть с тем же числом на
    # странице своего раздела. Сверка на РЕНДЕРЕННОМ HTML (как в проверке
    # 11), а не повторным вызовом той же функции — иначе проверка ловит
    # только "функция вернула то же самое себе", а не "два разных
    # шаблона согласны" (тот же довод, что в докстринге проверки 11).
    status_html = urllib.request.urlopen("http://localhost:8000/status", timeout=15).read().decode("utf-8")
    rsk_dashboard_html = urllib.request.urlopen("http://localhost:8000/rsk/dashboard", timeout=15).read().decode("utf-8")

    # СМР: "% выполнено" — /dashboard (hero-ring-num) vs /status (kpi-num,
    # подпись "Прогресс, взвешенный по трудоёмкости").
    dash_pct_m = re.search(r'<div class="hero-ring-num">(\d+(?:[.,]\d+)?)%</div>', dashboard_html)
    status_pct_m = re.search(
        r'<div class="kpi-num">([^<]*?)%</div>\s*<div class="kpi-label">Прогресс, взвешенный по трудоёмкости</div>',
        status_html,
    )
    check(
        "% выполнено работ: /dashboard (hero-ring) vs /status",
        "/dashboard", dash_pct_m.group(1) if dash_pct_m else None,
        "/status", status_pct_m.group(1).strip() if status_pct_m else None,
    )

    # СМР: прогноз завершения — /dashboard (hero-stat "Срок") vs /status
    # (kpi-num, подпись "Прогноз завершения по фактическому темпу"). Тот
    # же forecast_pace_date = crit["forecast_date"] (см. докстринг
    # get_scurve_data) — здесь сверяется не формула, а то, что оба
    # шаблона рисуют одну и ту же дату на экране.
    dash_forecast_m = re.search(
        r'<span class="hero-stat-label">Срок</span>\s*<span class="hero-stat-value[^"]*">([\d.]+)',
        dashboard_html,
    )
    status_forecast_m = re.search(
        r'<div class="kpi-num" style="font-size:1\.4rem">([\d.]+)</div>\s*<div class="kpi-label">Прогноз завершения по фактическому темпу</div>',
        status_html,
    )
    check(
        "Прогноз завершения: /dashboard (Срок) vs /status",
        "/dashboard", dash_forecast_m.group(1) if dash_forecast_m else None,
        "/status", status_forecast_m.group(1) if status_forecast_m else None,
    )

    # ИД: деньги — ТЗ Якименко А.И. №10, 23.09.2026 (п.3/4) — пять денежных
    # тайлов ИДЕНТИЧНЫ на /dashboard и /id-folders (одна и та же подпись,
    # одно и то же число). Сверяем все пять тайл-в-тайл на отрендеренном HTML.
    id_money_tile_labels = [
        "Всего по контракту с НДС, ₽", "Подписано по КС-3 с НДС, ₽", "Подписано ИД с НДС, ₽",
        "Невыбираемый остаток с НДС, ₽", "Остаток по контракту с НДС, ₽",
    ]
    id_money_tile_values = {}
    for label in id_money_tile_labels:
        pattern = re.compile(
            r'<div class="kpi-num[^"]*">([^<]+)</div>\s*<div class="kpi-label">' + re.escape(label) + r"</div>"
        )
        dash_m = pattern.search(dashboard_html)
        folders_m = pattern.search(id_folders_html)
        dash_v = _parse_ru_money(dash_m.group(1).strip()) if dash_m else None
        folders_v = _parse_ru_money(folders_m.group(1).strip()) if folders_m else None
        id_money_tile_values[label] = dash_v
        check(
            f"ИД, тайл «{label}»: /dashboard vs /id-folders",
            "/dashboard", round(dash_v, 2) if dash_v is not None else None,
            "/id-folders", round(folders_v, 2) if folders_v is not None else None,
            tolerance=0.01,
        )

    # ИД: «Остаток по контракту с НДС, ₽» = «Всего по контракту» −
    # «Подписано по КС-3» − «Невыбираемый остаток» (ТЗ №10, п.3 — формула
    # больше не вычитает «Подписано ИД», КС-3 и «Подписано ИД» — одни и те
    # же деньги с двух сторон) — как реально нарисовано на /dashboard.
    contract_v = id_money_tile_values["Всего по контракту с НДС, ₽"]
    ks3_tile_v = id_money_tile_values["Подписано по КС-3 с НДС, ₽"]
    manual_tile_v = id_money_tile_values["Невыбираемый остаток с НДС, ₽"]
    remaining_contract_v = id_money_tile_values["Остаток по контракту с НДС, ₽"]
    computed_remaining = (
        contract_v - ks3_tile_v - manual_tile_v
        if None not in (contract_v, ks3_tile_v, manual_tile_v) else None
    )
    check(
        "ИД: «Остаток по контракту с НДС, ₽» = «Всего по контракту» − «Подписано по КС-3» − «Невыбираемый остаток» (как на экране /dashboard)",
        "пересчитано из тайлов", round(computed_remaining, 2) if computed_remaining is not None else None,
        "тайл «Остаток по контракту с НДС, ₽»", round(remaining_contract_v, 2) if remaining_contract_v is not None else None,
        tolerance=0.01,
    )

    # ИД: труба «Выполнение» убрана с /dashboard (ТЗ №10, п.2) — стадия
    # «Текущая КС-2» теперь сверяется только на /id-folders, где труба
    # осталась, против тайла «Подписано ИД» (та же сумма ks2_sum, что
    # раньше называлась «Подписано по КС-2»).
    ks2_tile_v = id_money_tile_values["Подписано ИД с НДС, ₽"]
    ks2_current_label = m.ID_FOLDER_PIPE_STAGE_LABELS["ks2_current"]
    folders_ks2_pipe_money_m = re.search(
        r'<span class="id-pipe-label"[^>]*>' + re.escape(ks2_current_label) + r"</span>\s*"
        r'<span class="id-pipe-money"[^>]*>([^<]+)</span>',
        id_folders_html,
    )
    # «— ₽» на трубе (known_sum_count=0) означает настоящую сумму 0, ту же,
    # что coalesce(sum(...),0) в тайле — сравниваем как 0 (внутренняя
    # сверка тождества, не то, что видит пользователь).
    _ks2_pipe_raw = folders_ks2_pipe_money_m.group(1).replace("₽", "").strip() if folders_ks2_pipe_money_m else None
    pipe_ks2_money_v = 0.0 if _ks2_pipe_raw == "—" else _parse_ru_money(_ks2_pipe_raw)
    check(
        "ИД: труба «Текущая КС-2» (₽) = тайл «Подписано ИД с НДС, ₽» (на /id-folders)",
        "труба, Текущая КС-2", round(pipe_ks2_money_v, 2) if pipe_ks2_money_v is not None else None,
        "тайл", round(ks2_tile_v, 2) if ks2_tile_v is not None else None,
        tolerance=0.01,
    )

    # ИД: «Поток по вкладкам» — ТЗ №10, п.2 — тот же партиал
    # (_id_progress_stream.html) над той же функцией
    # (compute_id_progress_stream()) на /dashboard и на /id-progress,
    # числа обязаны совпадать буквально построчно, не просто «похоже».
    stream_bar_re = re.compile(
        r'<div class="bar-label">([^<]+)</div>.*?<div class="bar-value nowrap">([^<]+)</div>', re.S
    )
    dash_stream_rows = stream_bar_re.findall(dashboard_html)
    progress_stream_rows = stream_bar_re.findall(id_progress_html)
    check(
        "«Поток по вкладкам»: число строк на /dashboard vs /id-progress",
        "/dashboard", len(dash_stream_rows),
        "/id-progress", len(progress_stream_rows),
    )
    for (dash_label, dash_val), (prog_label, prog_val) in zip(dash_stream_rows, progress_stream_rows):
        check(
            f"«Поток по вкладкам», вкладка «{dash_label}»: /dashboard vs /id-progress",
            "/dashboard", f"{dash_label}: {dash_val}",
            "/id-progress", f"{prog_label}: {prog_val}",
        )

    # РСК: ТЗ Якименко А.И., 16.09.2026, §1 — шесть плиток, ОДИНАКОВЫЕ
    # подпись и число на /dashboard и /rsk/dashboard (было — разные
    # подписи и одна плитка "Заблокировано" на /dashboard, сложенная из
    # двух чисел /rsk/dashboard; теперь оба экрана рисуют один и тот же
    # compute_rsk_dashboard_stats() тайл-в-тайл, как и для денег ИД
    # выше). Сверяем все шесть по отрендеренному HTML.
    rsk_tile_labels = [
        "Активных замечаний", "Готовы к снятию", "Предъявить ИД",
        "Корректировка ПД/РД", "Отклонено РСК", "Устранено",
    ]
    for label in rsk_tile_labels:
        pattern = re.compile(
            r'<div class="kpi-num[^"]*">(\d+)</div>\s*<div class="kpi-label">' + re.escape(label) + r"</div>"
        )
        dash_m = pattern.search(dashboard_html)
        rsk_m = pattern.search(rsk_dashboard_html)
        check(
            f"РСК, тайл «{label}»: /dashboard vs /rsk/dashboard",
            "/dashboard", int(dash_m.group(1)) if dash_m else None,
            "/rsk/dashboard", int(rsk_m.group(1)) if rsk_m else None,
        )

    # --- ТЗ Якименко А.И., 16.09.2026, задача 3, §3/§7.7 — до этой задачи
    # экран смены не писал сроки графика вообще (только /gantt писал
    # current_schedule; форма ввода факта писала мёртвый work.plan_-
    # finish_date, который никто не читал) — отсюда и был найденный
    # разрыв "новый срок не двигает график". Проверка ловит именно
    # повторение этого класса дефекта: сроки, которые видит /api/shift
    # (правятся прямо в его таблице и в модалке «Ввод факта»), обязаны
    # буквально совпадать с тем, что рисует /api/gantt — оба читают
    # одну и ту же current_schedule, второй независимой записи нет.
    shift_data = json.loads(
        urllib.request.urlopen("http://localhost:8000/api/shift?all=1", timeout=15).read().decode("utf-8")
    )
    gantt_data = json.loads(
        urllib.request.urlopen("http://localhost:8000/api/gantt?days=7", timeout=15).read().decode("utf-8")
    )
    shift_sched = {it["id"]: (it["current_start"], it["current_finish"]) for it in shift_data["items"]}
    gantt_sched = {}
    for g in gantt_data["groups"]:
        for w in g["works"]:
            gantt_sched[w["id"]] = (w["current_start"], w["current_finish"])
    common_ids = set(shift_sched) & set(gantt_sched)
    mismatched = sorted(wid for wid in common_ids if shift_sched[wid] != gantt_sched[wid])
    check(
        f"Сроки графика (current_schedule): /api/shift vs /api/gantt — по всем {len(common_ids)} работам"
        + (f" (расходятся: work_id {mismatched[:5]})" if mismatched else ""),
        "количество расхождений", len(mismatched),
        "ожидается", 0,
    )

    # --- Задание координатора 18.09.2026, §4 — механическая защита от
    # повторения дефекта KNOWN_ISSUES.md §53: триггер `daily_progress_-
    # audit_trap_trg` полтора суток молча ссылался на несуществующую
    # таблицу (её унесло в схему `archive` вместе с реальными backup-
    # таблицами при уборке 15.09.2026) — обнаружено случайно, при
    # постороннем откате тестовой записи, не проверкой. Два слоя:
    # (а) статический — читает текст КАЖДОЙ триггерной функции в БД и
    # проверяет, что каждая таблица, в которую она пишет
    # (insert into/update/delete from), реально существует — ловит
    # будущий такой же триггер на любой другой таблице, не только на
    # daily_progress; (б) живой — реальный UPDATE+ROLLBACK на самой
    # daily_progress, тем же путём, каким чинился и проверялся дефект
    # (ничего не остаётся в БД — транзакция обязательно откатывается).
    trigger_funcs = m.query("""
        select distinct p.oid::regprocedure::text as func, pg_get_functiondef(p.oid) as src
        from pg_trigger t
        join pg_proc p on p.oid = t.tgfoid
        where not t.tgisinternal
    """)
    known_relations = {
        r["relname"] for r in m.query(
            "select relname from pg_class where relkind in ('r','v','m','p') "
            "and relnamespace = (select oid from pg_namespace where nspname='public')"
        )
    }
    ref_pattern = re.compile(
        r'\b(?:insert\s+into|update|delete\s+from)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?', re.IGNORECASE
    )
    missing_refs = []
    for row in trigger_funcs:
        for tbl in set(m.group(1) for m in ref_pattern.finditer(row["src"])):
            if tbl not in known_relations:
                missing_refs.append(f"{row['func']} -> {tbl}")
    check(
        "Триггерные функции не ссылаются на несуществующие таблицы (статический разбор текста функции)",
        "найдено ссылок на отсутствующие таблицы", len(missing_refs),
        "ожидается", 0,
    )
    if missing_refs:
        print("  Подробности:", "; ".join(missing_refs))

    # (б) живая проверка — та же таблица, тот же класс операции (UPDATE),
    # что уронил прод 16.09.2026; выполняется и тут же откатывается.
    conn = db.get_conn()
    live_error = None
    try:
        with conn.cursor() as cur:
            cur.execute("select id from daily_progress limit 1")
            sample = cur.fetchone()
            if sample:
                cur.execute(
                    "update daily_progress set updated_at = updated_at where id = %s",
                    (sample["id"],),
                )
    except Exception as e:  # noqa: BLE001 — фиксируем факт ошибки для отчёта, не даём ей всплыть
        live_error = str(e).strip()
    finally:
        conn.rollback()
        conn.close()
    check(
        "Живая проверка: UPDATE на daily_progress проходит без ошибки БД (откатывается, ничего не остаётся)",
        "ошибка", live_error or "нет",
        "ожидается", "нет",
    )

    # Координатор, 21.09.2026, задание про легенду «Потока по вкладкам»:
    # три числа (подписано/в цикле РСК/остальное) на /id-progress и
    # плитка «Связок раздел+вид работы с записью» наверху той же
    # страницы считаются РАЗНЫМИ запросами (compute_id_progress_stream()
    # против compute_id_progress_tiles()) — до сих пор ничего не
    # проверяло, что они говорят об одном и том же. На 21.09.2026
    # совпадают (2872=2872) — проверка ловит расхождение, если оно
    # появится позже, не переоткрывает уже закрытый вопрос сейчас.
    stream_rows = m.compute_id_progress_stream()
    stream_sum = sum(r["green_n"] + r["yellow_n"] + r["red_n"] for r in stream_rows)
    tiles = m.compute_id_progress_tiles()
    check(
        "Поток по вкладкам: сумма зелёный+жёлтый+красный по всем вкладкам = плитка «Связок раздел+вид работы с записью»",
        "сумма по потоку", stream_sum,
        "плитка total_pairs", tiles["total_pairs"],
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
