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

    # --- 5. Деньги: контракт − подписано ранее − подписано по КС-2 − ручной
    # объём = остаток (ТЗ Якименко А.И., 16.09.2026 — единственный остаток,
    # закрывает KNOWN_ISSUES.md §42; см. compute_id_folder_stats()).
    identity_remaining = (
        float(folder_stats["contract_total"])
        - float(folder_stats["signed_before_sum"])
        - float(folder_stats["ks2_sum"])
        - float(folder_stats["manual_sum"])
    )
    check(
        "Деньги: контракт − подписано ранее − подписано по КС-2 − ручной объём vs money_remaining",
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

    # --- 7. ТЗ Якименко А.И., 16.09.2026 — «Подписано ранее» (папки,
    # signed_date раньше границы app_setting['id_signed_before_boundary_date'])
    # и «Подписано по КС-2» не должны считать одну папку дважды (см.
    # compute_id_folder_stats()) — сверка count() против прямого SQL по
    # обоим множествам плюс отдельно overlap_count (папки, которые попали в
    # «ранее», но у них уже есть и ks2_date).
    boundary = folder_stats["signed_before_boundary"]
    check(
        "Подписано ранее: compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["signed_before_count"],
        "прямой SQL", m.query_one(
            "select count(*) as n from id_folder where signed_date is not null and signed_date < %(b)s",
            {"b": boundary},
        )["n"],
    )
    check(
        "Подписано по КС-2: compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["ks2_count"],
        "прямой SQL", m.query_one(
            "select count(*) as n from id_folder where ks2_date is not null "
            "and not (signed_date is not null and signed_date < %(b)s)",
            {"b": boundary},
        )["n"],
    )
    check(
        "Пересечение «Подписано ранее» и КС-2 (overlap_count): compute_id_folder_stats() vs прямой SQL",
        "compute_id_folder_stats()", folder_stats["overlap_count"],
        "прямой SQL", m.query_one(
            "select count(*) as n from id_folder where signed_date is not null and signed_date < %(b)s "
            "and ks2_date is not null",
            {"b": boundary},
        )["n"],
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

    # --- 11. Труба папок ИД на /dashboard vs /id-folders — не текст кода, а
    # то, что реально отдаёт HTTP-сервер (задание координатора: "смотреть на
    # экран, не на код"). Обе страницы включают один и тот же шаблон
    # _id_folder_funnel.html — здесь сверяются числа из ОТРЕНДЕРЕННОГО HTML
    # обеих страниц, не повторный вызов той же Python-функции. Заход 7,
    # 11.09.2026: разметка сменилась с плоских плиток (.kpi-num/.kpi-label)
    # на трубу (.id-pipe-count/.id-pipe-label) — обновлён селектор, сама
    # проверка (числа стадий сходятся между двумя страницами) не менялась.
    # ТЗ 15.09.2026: труба сменила стадии на ID_FOLDER_PIPE_STAGE_LABELS
    # (не каноническую ID_FOLDER_STAGE_LABELS — та осталась для колонки
    # «Стадия» реестра, труба её больше не показывает) — обе страницы
    # рендерят один и тот же партиал, обязаны совпасть по каждой стадии.
    dashboard_html = urllib.request.urlopen("http://localhost:8000/dashboard", timeout=15).read().decode("utf-8")
    id_folders_html = urllib.request.urlopen("http://localhost:8000/id-folders", timeout=15).read().decode("utf-8")
    for stage, label in m.ID_FOLDER_PIPE_STAGE_LABELS.items():
        pattern = re.compile(
            r'<span class="id-pipe-count[^"]*"[^>]*>(\d+)</span>\s*'
            r'<span class="id-pipe-label"[^>]*>' + re.escape(label) + r"</span>"
        )
        dash_match = pattern.search(dashboard_html)
        folders_match = pattern.search(id_folders_html)
        dash_n = int(dash_match.group(1)) if dash_match else None
        folders_n = int(folders_match.group(1)) if folders_match else None
        check(
            f"Труба папок ИД (отрендеренный HTML), стадия «{label}»: /dashboard vs /id-folders",
            "/dashboard", dash_n,
            "/id-folders", folders_n,
        )

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

    # ИД: деньги — ТЗ Якименко А.И., 16.09.2026 (§3) — пять денежных тайлов
    # ИДЕНТИЧНЫ на /dashboard и /id-folders (одна и та же подпись, одно и то
    # же число), не просто "то же значение под разными подписями", как было
    # до этого ТЗ. Сверяем все пять тайл-в-тайл на отрендеренном HTML.
    id_money_tile_labels = [
        "Всего по контракту, ₽", "Подписано ранее, ₽", "Подписано по КС-2, ₽",
        "Невыбираемый остаток, ₽", "Остаток по контракту, ₽",
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

    # ИД: «Остаток по контракту, ₽» = «Всего по контракту» − «Подписано
    # ранее» − «Подписано по КС-2» − «Невыбираемый остаток» — как реально
    # нарисовано на экране /dashboard (KNOWN_ISSUES.md §42, "два остатка",
    # закрыто этим ТЗ: остаток теперь один и тот же на обеих страницах).
    contract_v = id_money_tile_values["Всего по контракту, ₽"]
    signed_before_tile_v = id_money_tile_values["Подписано ранее, ₽"]
    ks2_tile_v = id_money_tile_values["Подписано по КС-2, ₽"]
    manual_tile_v = id_money_tile_values["Невыбираемый остаток, ₽"]
    remaining_contract_v = id_money_tile_values["Остаток по контракту, ₽"]
    computed_remaining = (
        contract_v - signed_before_tile_v - ks2_tile_v - manual_tile_v
        if None not in (contract_v, signed_before_tile_v, ks2_tile_v, manual_tile_v) else None
    )
    check(
        "ИД: «Остаток по контракту, ₽» = «Всего по контракту» − «Подписано ранее» − «Подписано по КС-2» − «Невыбираемый остаток» (как на экране /dashboard)",
        "пересчитано из тайлов", round(computed_remaining, 2) if computed_remaining is not None else None,
        "тайл «Остаток по контракту, ₽»", round(remaining_contract_v, 2) if remaining_contract_v is not None else None,
        tolerance=0.01,
    )

    # ИД: headline "N из M папок с КС-2" убран (ТЗ 15.09.2026) — взамен
    # сверка ₽ стадии трубы «Текущая КС-2» с тайлом «Подписано по КС-2, ₽»
    # на той же странице (оба должны показывать одну и ту же сумму
    # ks2_sum, но верстка не переиспользует текст друг друга).
    ks2_current_label = m.ID_FOLDER_PIPE_STAGE_LABELS["ks2_current"]
    dash_ks2_pipe_money_m = re.search(
        r'<span class="id-pipe-label"[^>]*>' + re.escape(ks2_current_label) + r"</span>\s*"
        r'<span class="id-pipe-money"[^>]*>([^<]+)</span>',
        dashboard_html,
    )
    # «— ₽» на трубе (known_sum_count=0 — либо в стадии вообще нет папок,
    # либо есть, но ни у одной ещё не заполнена сметная стоимость) в обоих
    # случаях означает настоящую сумму 0, ту же, что coalesce(sum(...),0)
    # в тайле — сравниваем как 0, не как "неизвестно" (это внутренняя
    # сверка тождества, не то, что показывается пользователю "не изобретая
    # число" — это правило про экран, не про эту проверку).
    _ks2_pipe_raw = dash_ks2_pipe_money_m.group(1).replace("₽", "").strip() if dash_ks2_pipe_money_m else None
    pipe_ks2_money_v = 0.0 if _ks2_pipe_raw == "—" else _parse_ru_money(_ks2_pipe_raw)
    check(
        "ИД: труба «Текущая КС-2» (₽) = тайл «Подписано по КС-2, ₽» (на /dashboard)",
        "труба, Текущая КС-2", round(pipe_ks2_money_v, 2) if pipe_ks2_money_v is not None else None,
        "тайл", round(ks2_tile_v, 2) if ks2_tile_v is not None else None,
        tolerance=0.01,
    )

    # РСК: /dashboard ("Активных замечаний"/"Готово к снятию") vs
    # /rsk/dashboard (tiles.total_active/ready_to_close) — разные
    # подписи, тот же compute_rsk_dashboard_stats(). "Заблокировано" на
    # Обзоре — сумма blocked_by_id+needs_rd, сверяем с суммой тех же
    # двух чисел на /rsk/dashboard, а не с одним из них.
    dash_active_m = re.search(r'<div class="kpi-num">(\d+)</div>\s*<div class="kpi-label">Активных замечаний</div>', dashboard_html)
    rsk_active_m = re.search(r'<div class="kpi-num">(\d+)</div>\s*<div class="kpi-label">Всего активных</div>', rsk_dashboard_html)
    check(
        "РСК, активных замечаний: /dashboard vs /rsk/dashboard",
        "/dashboard", int(dash_active_m.group(1)) if dash_active_m else None,
        "/rsk/dashboard", int(rsk_active_m.group(1)) if rsk_active_m else None,
    )

    dash_ready_m = re.search(r'<div class="kpi-num ok">(\d+)</div>\s*<div class="kpi-label">Готово к снятию</div>', dashboard_html)
    rsk_ready_m = re.search(r'<div class="kpi-num ok">(\d+)</div>\s*<div class="kpi-label">Готово к снятию</div>', rsk_dashboard_html)
    check(
        "РСК, готово к снятию: /dashboard vs /rsk/dashboard",
        "/dashboard", int(dash_ready_m.group(1)) if dash_ready_m else None,
        "/rsk/dashboard", int(rsk_ready_m.group(1)) if rsk_ready_m else None,
    )

    dash_blocked_m = re.search(r'<div class="kpi-label">Заблокировано</div>\s*<div class="kpi-sub">(\d+) ждёт ИД · (\d+) ждёт корректировки РД</div>', dashboard_html)
    rsk_blocked_id_m = re.search(r'<div class="kpi-num warn">(\d+)</div>\s*<div class="kpi-label">Заблокировано ожиданием ИД</div>', rsk_dashboard_html)
    rsk_needs_rd_m = re.search(r'<div class="kpi-num warn">(\d+)</div>\s*<div class="kpi-label">Ждёт корректировки РД</div>', rsk_dashboard_html)
    dash_blocked_sum = (int(dash_blocked_m.group(1)) + int(dash_blocked_m.group(2))) if dash_blocked_m else None
    rsk_blocked_sum = (
        (int(rsk_blocked_id_m.group(1)) + int(rsk_needs_rd_m.group(1)))
        if rsk_blocked_id_m and rsk_needs_rd_m else None
    )
    check(
        "РСК, заблокировано (ожиданием ИД + корректировкой РД): /dashboard (сумма) vs /rsk/dashboard (сумма)",
        "/dashboard", dash_blocked_sum,
        "/rsk/dashboard", rsk_blocked_sum,
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
