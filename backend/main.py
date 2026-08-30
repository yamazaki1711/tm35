import json
import os
import re
import urllib.parse
from datetime import date as date_cls, timedelta

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import query, query_one, execute, run_in_transaction
from analytics import (
    compute_overdue, compute_project_forecast, compute_resource_deficit, DONE_STATUSES,
    compute_work_weight, compute_weighted_progress, compute_evm, compute_ppc,
    compute_required_people, compute_forecast_from_people, compute_forecast_by_pace,
)

app = FastAPI(title="ТМ-35 Мониторинг")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Русские подписи вместо кодов схемы БД — жалоба координатора: "почему так
# много слов на английском". Коды остаются в БД (для запросов/аналитики),
# в интерфейсе — только перевод, через Jinja-фильтры ниже.
RU_STATUS = {
    "not_started": "не начата", "in_progress": "в работе", "suspended": "приостановлена",
    "limited": "ограничена", "done_physically": "выполнена физически", "submitted": "предъявлена",
    "accepted": "принята", "closed": "закрыта", "cancelled": "отменена",
}
RU_SOURCE = {"main": "основной график", "iks": "замечания ИКС", "rsk": "замечания РСК", "aux": "вспомогательные"}
RU_EXECUTOR = {"own_forces": "свои силы", "subcontract": "субподряд"}
RU_BLOCKER_TYPE = {
    "material": "материал", "delivery": "поставка", "equipment": "техника", "fuel": "ГСМ",
    "weather": "погода", "front": "фронт работ", "design_decision": "проектное решение",
    "subcontract": "субподряд", "contract": "договор", "payment": "оплата",
    "acceptance": "приёмка", "sequence": "очерёдность", "aux_reallocation": "переброска на вспом. работы",
}
RU_BLOCKER_STATUS = {"active": "активно", "resolved": "снято"}
RU_DATA_QUALITY = {"ok": "ок", "needs_review": "проверить"}
RU_MATERIAL_STATUS = {
    "requested": "заявка", "ordered": "заказан", "paid": "оплачен",
    "in_transit": "в пути", "on_site": "на объекте", "missing": "отсутствует",
}
RU_DP_SOURCE = {"excel_import": "из Excel", "web_form": "веб-форма"}
REASON_CODES = [
    ("WEATHER_RAIN", "Дождь"),
    ("WEATHER_WIND", "Ветер"),
    ("WEATHER_TEMP", "Температурные ограничения"),
    ("FUEL_MISSING", "Отсутствие ГСМ"),
    ("MATERIAL_MISSING", "Отсутствие материала"),
    ("MATERIAL_DELIVERY", "Задержка поставки материала"),
    ("EQUIPMENT_MISSING", "Отсутствие техники"),
    ("EQUIPMENT_BROKEN", "Поломка техники"),
    ("FRONT_MISSING", "Отсутствие фронта работ"),
    ("DESIGN_MISSING", "Отсутствие проектного решения"),
    ("SUBCONTRACT_MISSING", "Субподрядчик не включился"),
    ("CONTRACT_NOT_SIGNED", "Договор не подписан"),
    ("PAYMENT_MISSING", "Отсутствие оплаты"),
    ("AUX_REALLOCATION", "Переброска на вспомогательные работы"),
    ("ACCEPTANCE_WAIT", "Ожидание приёмки/предъявления"),
    ("SEQUENCE_WAIT", "Ожидание предыдущей работы"),
    ("PLANNING_ERROR", "Ошибка планирования"),
    ("OTHER", "Иное (указать в комментарии)"),
]
REASON_CODE_SET = {c for c, _ in REASON_CODES}
RU_REASON_CODE = dict(REASON_CODES)
templates.env.filters["ru_status"] = lambda v: RU_STATUS.get(v, v)
templates.env.filters["ru_source"] = lambda v: RU_SOURCE.get(v, v)
templates.env.filters["ru_executor"] = lambda v: RU_EXECUTOR.get(v, v)
templates.env.filters["ru_blocker_type"] = lambda v: RU_BLOCKER_TYPE.get(v, v)
templates.env.filters["ru_blocker_status"] = lambda v: RU_BLOCKER_STATUS.get(v, v)
templates.env.filters["ru_material_status"] = lambda v: RU_MATERIAL_STATUS.get(v, v)
templates.env.filters["ru_dp_source"] = lambda v: RU_DP_SOURCE.get(v, v)
templates.env.filters["ru_reason_code"] = lambda v: RU_REASON_CODE.get(v, v)

# Импортёр (import/load_to_postgres.py) хранит служебный префикс в
# blocker.description для идемпотентной перезагрузки (см. DAY_BLOCKER_MARKER
# там же) — пользователю он виден быть не должен, только сам текст причины.
# "WORK:" — не наш префикс, а часть исходного текста комментария в самом
# Excel-файле (см. .claude/skills/tm35-excel/SKILL.md, "источник стоп-
# факторов и WORK:-пометок") — тоже служебная пометка, тоже не для показа.
_DAY_BLOCKER_MARKER = "[день-уровень, Excel]: "
_WORK_PREFIX_RE = re.compile(r"^WORK:\s*")


def _strip_source_marker(v):
    if not v:
        return v
    if v.startswith(_DAY_BLOCKER_MARKER):
        v = v[len(_DAY_BLOCKER_MARKER):]
    v = _WORK_PREFIX_RE.sub("", v)
    return v


templates.env.filters["strip_source_marker"] = _strip_source_marker


def _dmy(value):
    """Единый формат отображения дат по всему интерфейсу — ДД.ММ.ГГГГ.
    Хранение остаётся ISO (в БД и в скрытых полях форм), фильтр только
    для вывода. Принимает date/datetime, ISO-строку или пусто."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = date_cls.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y")


templates.env.filters["dmy"] = _dmy

# "Последняя запись побеждает" между excel_import и web_form за один
# (дата, работа) — обе строки физически остаются в daily_progress
# (unique включает source), эта CTE выбирает победителя для отображения.
LATEST_DP_CTE = """
with latest_dp as (
    select distinct on (date, work_id) *
    from daily_progress
    order by date, work_id, updated_at desc
)
"""

WEB_FORM_USER_NAME = "Веб-форма ТМ-35 (общий вход tm-35)"


def render(request, template, active, **ctx):
    return templates.TemplateResponse(request, template, {"active": active, **ctx})


def ensure_web_form_user():
    row = query_one("select id from app_user where full_name=%s", (WEB_FORM_USER_NAME,))
    if row:
        return row["id"]
    execute(
        "insert into app_user (full_name, role) values (%s, 'executor') "
        "on conflict do nothing",
        (WEB_FORM_USER_NAME,),
    )
    row = query_one("select id from app_user where full_name=%s", (WEB_FORM_USER_NAME,))
    return row["id"] if row else None


def get_remaining_effort():
    """
    Остаток трудоёмкости (чел-дни) ТОЛЬКО по работам с реальной
    трудоёмкостью из календарной матрицы — не выдумываем для остальных.
    Находка координатора: прежняя "требуемая численность" (сумма "Кол-во
    чел." по всем активным работам, без учёта прогресса) давала 272 при
    12 фактических — бессмысленно завышенное число. Правильная формула —
    остаток / рабочие дни до срока (backend/analytics.py, с тестами).
    """
    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as t "
        "from daily_progress where source='excel_import' and planned_crew is not null "
        "group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: float(r["t"]) for r in trudoemkost_rows}

    works = query("select id, fact_pct, status from work")
    remaining = 0.0
    known_count = 0
    excluded_count = 0
    for w in works:
        if w["status"] in DONE_STATUSES:
            continue
        t = trudoemkost_by_work.get(w["id"])
        if t is None:
            excluded_count += 1
            continue
        known_count += 1
        pct = float(w["fact_pct"]) if w["fact_pct"] is not None else 0.0
        remaining += t * (1 - pct / 100)
    return remaining, known_count, excluded_count


@app.get("/api/calculator")
def api_calculator(target_date: str = "", available_people: str = ""):
    remaining, known_count, excluded_count = get_remaining_effort()
    today = date_cls.today()
    result = {
        "remaining_effort_days": round(remaining, 1),
        "known_work_count": known_count,
        "excluded_work_count": excluded_count,
        "today": today.isoformat(),
    }
    if target_date.strip():
        try:
            td = date_cls.fromisoformat(target_date.strip())
        except ValueError:
            return JSONResponse({"error": "Некорректная дата"}, status_code=400)
        req, wd = compute_required_people(remaining, today, td)
        result.update({"mode": "date_to_people", "target_date": td.isoformat(), "working_days": wd, "required_people": req})
    elif available_people.strip():
        try:
            people = int(available_people.strip())
        except ValueError:
            return JSONResponse({"error": "Некорректное число людей"}, status_code=400)
        needed_days, forecast = compute_forecast_from_people(remaining, people, today)
        result.update({
            "mode": "people_to_date", "available_people": people,
            "working_days_needed": needed_days, "forecast_date": forecast.isoformat() if forecast else None,
        })
    return result


def get_evm_data():
    """
    EVM/PPC-слой (доменный разбор координатора v2.0, "Измерительный слой").
    Трудоёмкость = Σ planned_crew из календарной матрицы Excel — подтверждено
    сверкой с "Кол-во чел." на реальных данных (docs/REENGINEERING_LOG.md).
    Формулы и тесты — backend/analytics.py.
    """
    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]
    if not last_actual_date:
        return {"available": False}

    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as trudoemkost "
        "from daily_progress where source='excel_import' and planned_crew is not null "
        "group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: r["trudoemkost"] for r in trudoemkost_rows}

    works = query(
        """
        select w.id, w.code, w.fact_pct, bs.plan_crew as baseline_crew
        from work w
        left join baseline_schedule bs on bs.work_id = w.id
        """
    )
    weight_source_counts = {"trudoemkost": 0, "baseline_crew": 0, "flat": 0}
    weighted_input = []
    for w in works:
        weight, src = compute_work_weight(trudoemkost_by_work.get(w["id"]), w["baseline_crew"])
        weight_source_counts[src] += 1
        weighted_input.append({"code": w["code"], "weight": weight, "fact_pct": w["fact_pct"]})

    weighted_pct, total_weight, unknown_pct_count = compute_weighted_progress(weighted_input)

    bcws = query_one(
        LATEST_DP_CTE + "select sum(planned_crew) as v from latest_dp where date <= %s",
        (last_actual_date,),
    )["v"] or 0
    acwp = query_one(
        LATEST_DP_CTE + "select sum(actual_crew) as v from latest_dp where date <= %s",
        (last_actual_date,),
    )["v"] or 0
    total_trudoemkost = sum(trudoemkost_by_work.values()) if trudoemkost_by_work else 0
    bcwp = (weighted_pct or 0) / 100 * total_weight if total_weight else 0

    spi, cpi = compute_evm(bcws, acwp, bcwp)

    trailing_start = last_actual_date - timedelta(days=13)
    ppc_rows = query(
        LATEST_DP_CTE + "select planned_crew, actual_crew from latest_dp where date between %s and %s",
        (trailing_start, last_actual_date),
    )
    ppc_pct, ppc_promised, ppc_met = compute_ppc(ppc_rows)

    return {
        "available": True,
        "last_actual_date": last_actual_date,
        "trailing_start": trailing_start,
        "weighted_pct": weighted_pct,
        "total_weight": total_weight,
        "unknown_pct_count": unknown_pct_count,
        "weight_source_counts": weight_source_counts,
        "works_total": len(works),
        "bcws": bcws,
        "acwp": acwp,
        "bcwp": round(bcwp, 1),
        "total_trudoemkost": total_trudoemkost,
        "spi": spi,
        "cpi": cpi,
        "ppc_pct": ppc_pct,
        "ppc_promised": ppc_promised,
        "ppc_met": ppc_met,
    }


def get_criticality_data():
    """
    Общая для / и /critical выборка: критичность отставания, прогноз
    завершения, ресурсный дефицит. Формулы и обоснование — backend/analytics.py
    и docs/GAP_ANALYSIS.md (Цикл 1). Использует ТОЛЬКО данные из БД —
    ничего не придумывает; если временного baseline нет для работы, она
    просто не попадает в расчёт (не считается ни просроченной, ни в срок).
    """
    today = date_cls.today()

    works_with_baseline = query(
        """
        select w.code, w.name, w.status, bs.plan_finish
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_finish is not null and bs.confidence in ('high', 'medium')
        """
    )
    overdue = compute_overdue(works_with_baseline, today)

    active_finishes = [
        w["plan_finish"] for w in works_with_baseline
        if w["status"] not in DONE_STATUSES
    ]
    overdue_lags = [w["lag_days"] for w in overdue]
    forecast_date, avg_lag, baseline_date = compute_project_forecast(active_finishes, overdue_lags)

    # Запланировано И фактически вышло на ОДИН И ТОТ ЖЕ день (last_actual_date) —
    # раньше required_crew считался как сумма "Кол-во чел." по ВСЕМ активным
    # работам (272 при 12 фактических — бессмысленное число, не про этот день
    # и не про требуемую численность вообще, см. docs/REENGINEERING_LOG.md).
    # Требуемая численность под директивный срок — отдельно, /api/calculator.
    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]
    required_crew = actual_crew = None
    if last_actual_date:
        day_totals = query_one(
            LATEST_DP_CTE + "select sum(planned_crew) as p, sum(actual_crew) as a from latest_dp where date=%s",
            (last_actual_date,),
        )
        required_crew = day_totals["p"]
        actual_crew = day_totals["a"]

    deficit, coverage_pct = compute_resource_deficit(required_crew, actual_crew)

    return {
        "today": today,
        "overdue": overdue,
        "overdue_count": len(overdue),
        "works_with_baseline_count": len(works_with_baseline),
        "forecast_date": forecast_date,
        "avg_lag": avg_lag,
        "baseline_date": baseline_date,
        "required_crew": required_crew,
        "actual_crew": actual_crew,
        "deficit": deficit,
        "coverage_pct": coverage_pct,
        "last_actual_date": last_actual_date,
    }


def get_app_setting(key, default=None):
    row = query_one("select value from app_setting where key=%s", (key,))
    return row["value"] if row else default


def get_directive_deadline():
    v = get_app_setting("directive_deadline")
    if not v:
        return None
    try:
        return date_cls.fromisoformat(v)
    except ValueError:
        return None


def record_forecast_snapshot(today, forecast_date, method, remaining_effort_days, avg_daily_pace):
    """Копит снимок прогноза на текущую ISO-неделю (не чаще одного в неделю
    на метод) — не отдельный cron, снимок пишется при первом открытии
    /status на этой неделе. Если за неделю никто не откроет /status,
    снимка не будет — известное ограничение, см. migrations/004."""
    iso_year, iso_week, _ = today.isocalendar()
    execute(
        """
        insert into forecast_snapshot
            (snapshot_date, iso_year, iso_week, forecast_date, method, remaining_effort_days, avg_daily_pace)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (iso_year, iso_week, method) do nothing
        """,
        (today, iso_year, iso_week, forecast_date, method, remaining_effort_days, avg_daily_pace),
    )


def get_scurve_data():
    """
    Данные экрана "Успеваем?" (докладная координатора "что делают
    отраслевые системы", 16.08.2026): S-кривая план/факт нарастающим
    итогом из человеко-дней (та же трудоёмкость, что и в EVM-слое),
    гистограмма численности по дням, две независимые оценки прогноза
    (по темпу и по baseline+просрочке), тренд прогноза по неделям,
    дефицит ресурса до директивного срока.
    """
    today = date_cls.today()
    evm = get_evm_data()
    crit = get_criticality_data()
    remaining, known_count, excluded_count = get_remaining_effort()

    daily = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual
        from latest_dp
        group by date
        order by date
        """
    )
    series = []
    bcws_cum = 0.0
    acwp_cum = 0.0
    for r in daily:
        bcws_cum += float(r["planned"] or 0)
        if r["actual"] is not None:
            acwp_cum += float(r["actual"])
        series.append({
            "date": r["date"].isoformat(),
            "planned": float(r["planned"] or 0),
            "actual": float(r["actual"]) if r["actual"] is not None else None,
            "bcws_cum": round(bcws_cum, 1),
            "acwp_cum": round(acwp_cum, 1),
        })

    directive_deadline = get_directive_deadline()

    forecast_pace_date = avg_pace = None
    if evm.get("available"):
        recent_rows = query(
            LATEST_DP_CTE + """
            select date, sum(actual_crew) as v from latest_dp
            where actual_crew is not null
            group by date order by date desc limit 14
            """
        )
        recent_actuals = [float(r["v"] or 0) for r in recent_rows]
        forecast_pace_date, avg_pace, _ = compute_forecast_by_pace(remaining, recent_actuals, today)
        record_forecast_snapshot(today, forecast_pace_date, "pace", round(remaining, 1), avg_pace)
    if crit.get("forecast_date"):
        record_forecast_snapshot(today, crit["forecast_date"], "baseline_lag", None, None)

    trend_rows = query(
        "select snapshot_date, iso_year, iso_week, forecast_date, method "
        "from forecast_snapshot order by iso_year, iso_week"
    )
    trend = {"pace": [], "baseline_lag": []}
    for r in trend_rows:
        if r["forecast_date"] is None or r["method"] not in trend:
            continue
        trend[r["method"]].append({
            # Понедельник этой недели, не номер недели (правило проекта —
            # недели показываются датой, не ISO-номером).
            "week": date_cls.fromisocalendar(r["iso_year"], r["iso_week"], 1).isoformat(),
            "forecast_date": r["forecast_date"].isoformat(),
        })

    required_to_deadline = deadline_deficit = None
    if directive_deadline:
        required_to_deadline, working_days_to_deadline = compute_required_people(remaining, today, directive_deadline)
        deadline_deficit, _ = compute_resource_deficit(required_to_deadline, crit.get("actual_crew"))
    else:
        working_days_to_deadline = None

    deviation_days = None
    if directive_deadline and forecast_pace_date:
        deviation_days = (forecast_pace_date - directive_deadline).days

    return {
        "today": today,
        "evm": evm,
        "crit": crit,
        "remaining_effort_days": round(remaining, 1),
        "known_work_count": known_count,
        "excluded_work_count": excluded_count,
        "series": series,
        "total_trudoemkost": evm.get("total_trudoemkost", 0),
        "bcwp_point": evm.get("bcwp", 0),
        "forecast_pace_date": forecast_pace_date.isoformat() if forecast_pace_date else None,
        "avg_pace": avg_pace,
        "trend": trend,
        "directive_deadline": directive_deadline.isoformat() if directive_deadline else None,
        "required_to_deadline": required_to_deadline,
        "working_days_to_deadline": working_days_to_deadline,
        "deadline_deficit": deadline_deficit,
        "deviation_days": deviation_days,
    }


@app.post("/api/settings/directive-deadline")
def set_directive_deadline(value: str = Form("")):
    """JSON-ответ, не редирект — форма на /status сохраняет через fetch
    (нужно показать «Сохранено» и включить/выключить кнопку по факту
    изменения, редирект с перезагрузкой всей страницы это не даёт)."""
    value = value.strip()
    if value:
        try:
            date_cls.fromisoformat(value)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Некорректная дата."}, status_code=400)
    execute(
        "insert into app_setting (key, value, updated_at) values ('directive_deadline', %s, now()) "
        "on conflict (key) do update set value=excluded.value, updated_at=now()",
        (value or None,),
    )
    return {"ok": True, "value": value or None}


@app.get("/status")
def status_page(request: Request):
    data = get_scurve_data()
    chart_payload = {
        "series": data["series"],
        "total_trudoemkost": data["total_trudoemkost"],
        "bcwp_point": data["bcwp_point"],
        "last_actual_date": data["evm"].get("last_actual_date").isoformat() if data["evm"].get("last_actual_date") else None,
        "forecast_pace_date": data["forecast_pace_date"],
        "trend": data["trend"],
    }
    data["chart_json"] = json.dumps(chart_payload, ensure_ascii=False)
    return render(request, "status.html", "status", **data)


@app.get("/api/status-data")
def api_status_data():
    return get_scurve_data()


# ---------------------------------------------------------------------
# Экран "Что делать сегодня" (реинжиниринг v3, Цикл 2) — look-ahead,
# критичные работы по явному правилу, ограничения к снятию.
# ---------------------------------------------------------------------

def get_lookahead_works(today, horizon_days=14):
    """
    Работы, которые должны начаться в ближайшие horizon_days по baseline
    (только high/medium confidence — та же дисциплина, что и в /critical).
    "Что мешает начаться" — честно по тому, что реально есть в БД: сейчас
    это только "нет привязанного субподрядчика" (subcontractor_id) — реестры
    blocker/material не привязаны к конкретным работам (см.
    REENGINEERING_LOG.md, Цикл 2) и материал не проверяется, чтобы не
    придумывать сигнал, которого нет.
    """
    end = today + timedelta(days=horizon_days)
    rows = query(
        """
        select w.id, w.code, w.name, w.location, w.executor_type, w.subcontractor_id, bs.plan_start
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_start between %s and %s
          and bs.confidence in ('high', 'medium')
          and w.status = 'not_started'
        order by bs.plan_start, w.code
        """,
        (today, end),
    )
    result = []
    for w in rows:
        obstacles = []
        if w["executor_type"] == "subcontract" and w["subcontractor_id"] is None:
            obstacles.append("нет привязанного субподрядчика")
        result.append({**w, "obstacles": obstacles})
    return result


def get_critical_rule_works(today, directive_deadline, trudoemkost_by_work, last_actual_by_work):
    """
    Критичность по явному правилу (не абстрактный "приоритет 1..5" из
    Excel): работа попадает в список, если
      1) уже просрочена относительно baseline, ИЛИ
      2) плановое окончание позже директивного срока проекта, ИЛИ
      3) на неё напрямую висит неснятое ограничение (work_id указан).
    Для каждой — оценка требуемого темпа и нехватки людей ПО ЭТОЙ работе
    (тот же принцип, что и в общем калькуляторе Дата↔Люди, но на уровне
    одной работы вместо всего проекта).
    """
    works = query(
        """
        select w.id, w.code, w.name, w.fact_pct, w.status, bs.plan_finish
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_finish is not null and bs.confidence in ('high', 'medium')
        """
    )
    blocked_counts = {
        r["work_id"]: r["n"] for r in query(
            "select work_id, count(*) as n from blocker where work_id is not null and status='active' group by work_id"
        )
    }
    result = []
    for w in works:
        if w["status"] in DONE_STATUSES:
            continue
        reasons = []
        if w["plan_finish"] < today:
            reasons.append("просрочена относительно планового срока")
        if directive_deadline and w["plan_finish"] > directive_deadline:
            reasons.append("плановое окончание позже директивного срока проекта")
        blocked_n = blocked_counts.get(w["id"], 0)
        if blocked_n:
            reasons.append(f"висит {blocked_n} неснятых ограничений")
        if not reasons:
            continue

        additional_people = None
        trud = trudoemkost_by_work.get(w["id"])
        if trud is not None:
            pct = float(w["fact_pct"]) if w["fact_pct"] is not None else 0.0
            remaining_work = float(trud) * (1 - pct / 100)
            targets = [w["plan_finish"]] + ([directive_deadline] if directive_deadline else [])
            target = min(targets)
            required_pace, _ = compute_required_people(remaining_work, today, target)
            if required_pace is not None:
                current_pace = last_actual_by_work.get(w["id"], 0)
                additional_people = max(0, required_pace - current_pace)

        result.append({**w, "reasons": reasons, "additional_people": additional_people})
    return sorted(result, key=lambda x: -(x["additional_people"] or 0))


def get_today_data():
    today = date_cls.today()
    directive_deadline = get_directive_deadline()

    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as t from daily_progress "
        "where source='excel_import' and planned_crew is not null group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: r["t"] for r in trudoemkost_rows}

    last_actual_rows = query(
        LATEST_DP_CTE + """
        select distinct on (work_id) work_id, actual_crew
        from latest_dp where actual_crew is not null
        order by work_id, date desc
        """
    )
    last_actual_by_work = {r["work_id"]: r["actual_crew"] for r in last_actual_rows}

    lookahead = get_lookahead_works(today)
    critical = get_critical_rule_works(today, directive_deadline, trudoemkost_by_work, last_actual_by_work)
    open_blockers = query(
        "select id, blocker_type, description, created_at, expected_resolution_date, responsible_name, impact_days "
        "from blocker where status='active' order by created_at"
    )

    return {
        "today": today,
        "directive_deadline": directive_deadline.isoformat() if directive_deadline else None,
        "lookahead": lookahead,
        "critical": critical,
        "open_blockers": open_blockers,
    }


@app.get("/today")
def today_page(request: Request):
    return render(request, "today.html", "today", **get_today_data())


@app.post("/api/blocker/{blocker_id}")
def api_blocker_update(
    blocker_id: int,
    expected_resolution_date: str = Form(""),
    responsible_name: str = Form(""),
    resolve: str = Form(""),
):
    if resolve.strip():
        execute(
            "update blocker set status='resolved', actual_resolution_date=%s where id=%s",
            (date_cls.today(), blocker_id),
        )
    else:
        d = expected_resolution_date.strip()
        if d:
            try:
                date_cls.fromisoformat(d)
            except ValueError:
                return JSONResponse({"error": "Некорректная дата"}, status_code=400)
        execute(
            "update blocker set expected_resolution_date=%s, responsible_name=%s where id=%s",
            (d or None, responsible_name.strip() or None, blocker_id),
        )
    return RedirectResponse("/today", status_code=303)


# ---------------------------------------------------------------------
# Суточный рапорт как документ (реинжиниринг v3, Цикл 3) — не экран, а
# датированная страница, пригодная для печати/PDF (window.print()) и
# приложения к переписке с заказчиком. Формируется из уже введённого
# факта + двух ручных полей (погода, подпись), которых Excel не даёт.
# ---------------------------------------------------------------------

@app.get("/report")
def report_page(request: Request, date: str = ""):
    if not date.strip():
        last = query_one("select max(date) as d from daily_progress where actual_crew is not null")
        target_date = last["d"] if last and last["d"] else date_cls.today()
    else:
        try:
            target_date = date_cls.fromisoformat(date.strip())
        except ValueError:
            target_date = date_cls.today()

    by_location = query(
        LATEST_DP_CTE + """
        select coalesce(w.location, 'без участка') as location,
               sum(ldp.planned_crew) as planned, sum(ldp.actual_crew) as actual
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s
        group by coalesce(w.location, 'без участка')
        order by location
        """,
        (target_date,),
    )
    works_today = query(
        LATEST_DP_CTE + """
        select w.code, w.name, w.unit, ldp.planned_crew, ldp.actual_crew,
               ldp.done_volume, ldp.fact_pct, ldp.comment, ldp.reason_code, ldp.source
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s and (ldp.planned_crew is not null or ldp.actual_crew is not null)
        order by w.code
        """,
        (target_date,),
    )
    not_done = [
        r for r in works_today
        if (r["planned_crew"] or 0) > 0 and (r["actual_crew"] or 0) < (r["planned_crew"] or 0)
    ]
    blockers_arose = query(
        "select blocker_type, description from blocker where created_at::date = %s order by id",
        (target_date,),
    )
    blockers_resolved = query(
        "select blocker_type, description from blocker where actual_resolution_date = %s order by id",
        (target_date,),
    )
    meta = query_one("select weather, signed_by from daily_report_meta where date=%s", (target_date,))

    weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return render(
        request, "report.html", "report",
        target_date=target_date, date_label=f"{target_date.strftime('%d.%m.%Y')} ({weekday_names[target_date.weekday()]})",
        by_location=by_location, works_today=works_today, not_done=not_done,
        blockers_arose=blockers_arose, blockers_resolved=blockers_resolved,
        weather=meta["weather"] if meta else "", signed_by=meta["signed_by"] if meta else "",
    )


@app.post("/api/report-meta")
def api_report_meta(date: str = Form(...), weather: str = Form(""), signed_by: str = Form("")):
    execute(
        "insert into daily_report_meta (date, weather, signed_by, updated_at) values (%s, %s, %s, now()) "
        "on conflict (date) do update set weather=excluded.weather, signed_by=excluded.signed_by, updated_at=now()",
        (date, weather.strip() or None, signed_by.strip() or None),
    )
    return RedirectResponse(f"/report?date={date}", status_code=303)


# ---------------------------------------------------------------------
# "Почему отстаём" — аналитика потерь (реинжиниринг v3, Цикл 3)
# ---------------------------------------------------------------------

def get_losses_data(period_days=30):
    today = date_cls.today()
    start = today - timedelta(days=period_days)

    # Потери чел-дней по типу дня-уровневого ограничения. Один день может
    # нести несколько типов причин одновременно (ТЗ 11.4: "множественные
    # причины простоя в одной ячейке" — известный дефект исходных данных,
    # не подавляем его искусственным выбором "только одна причина").
    day_deficits = {
        r["date"]: max(0, float(r["planned"] or 0) - float(r["actual"] or 0))
        for r in query(
            LATEST_DP_CTE + """
            select date, sum(planned_crew) as planned, sum(actual_crew) as actual
            from latest_dp where date between %s and %s group by date
            """,
            (start, today),
        )
    }
    blocker_days = query(
        "select blocker_type, created_at::date as d from blocker where created_at::date between %s and %s",
        (start, today),
    )
    loss_by_type = {}
    overlap_days = 0
    seen_days = set()
    for b in blocker_days:
        loss_by_type.setdefault(b["blocker_type"], 0.0)
        loss_by_type[b["blocker_type"]] += day_deficits.get(b["d"], 0.0)
        if b["d"] in seen_days:
            overlap_days += 1
        seen_days.add(b["d"])

    # PPC по неделям
    ppc_rows = query(
        LATEST_DP_CTE + "select date, planned_crew, actual_crew from latest_dp where date between %s and %s",
        (start, today),
    )
    by_week = {}
    for r in ppc_rows:
        wk = r["date"].isocalendar()
        # Ключ — понедельник этой недели (ISO-дата сортируется так же, как
        # номер недели, но правило проекта требует показывать датой, не
        # номером — см. CLAUDE.md).
        key = date_cls.fromisocalendar(wk[0], wk[1], 1).isoformat()
        by_week.setdefault(key, []).append(r)
    ppc_by_week = []
    for wk in sorted(by_week):
        pct, promised, met = compute_ppc(by_week[wk])
        if promised:
            ppc_by_week.append({"week": wk, "ppc_pct": pct, "promised": promised, "met": met})

    # Срок снятия ограничений
    resolved = query(
        "select created_at::date as created, actual_resolution_date as resolved "
        "from blocker where status='resolved' and actual_resolution_date is not null"
    )
    resolution_days = [(r["resolved"] - r["created"]).days for r in resolved]
    avg_resolution_days = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    open_blocker_ages = [
        (today - r["created_at"].date()).days
        for r in query("select created_at from blocker where status='active'")
    ]

    # Отвлечение ресурса: сколько чел-дней факта ушло не на main
    by_source = query(
        """
        select w.source, sum(dp.planned_crew) as planned, sum(dp.actual_crew) as actual
        from daily_progress dp join work w on w.id = dp.work_id
        where dp.source = 'excel_import'
        group by w.source
        """
    )

    return {
        "period_days": period_days,
        "start": start,
        "today": today,
        "loss_by_type": sorted(
            [{"type": k, "days": round(v, 1)} for k, v in loss_by_type.items()],
            key=lambda x: -x["days"],
        ),
        "overlap_days": overlap_days,
        "ppc_by_week": ppc_by_week,
        "avg_resolution_days": avg_resolution_days,
        "resolved_count": len(resolution_days),
        "open_blocker_count": len(open_blocker_ages),
        "open_blocker_avg_age": round(sum(open_blocker_ages) / len(open_blocker_ages), 1) if open_blocker_ages else None,
        "by_source": [
            {
                "source": r["source"],
                "planned": float(r["planned"] or 0),
                "actual": float(r["actual"] or 0),
            }
            for r in by_source
        ],
    }


@app.get("/losses")
def losses_page(request: Request, period_days: int = 30):
    return render(request, "losses.html", "losses", **get_losses_data(period_days))


# ---------------------------------------------------------------------
# Главная
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Главный экран — ежедневный ввод факта. Простой список того, что
# запланировано на дату, без навигации и терминов (жалоба координатора:
# инженер ПТО 60 лет терялся в панели для аналитиков). Полная панель —
# /dashboard, отдельная малозаметная ссылка внизу.
# ---------------------------------------------------------------------

@app.get("/")
def simple_home(request: Request, date: str = ""):
    today = date_cls.today()
    if date.strip():
        try:
            target_date = date_cls.fromisoformat(date.strip())
        except ValueError:
            target_date = today
    else:
        target_date = today

    rows = query(
        LATEST_DP_CTE + """
        select ldp.work_id, w.code, w.name, w.location, ldp.planned_crew, ldp.actual_crew, ldp.reason_code,
               ldp.comment
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s and (ldp.planned_crew is not null and ldp.planned_crew > 0)
        order by w.code
        """,
        (target_date,),
    )
    all_works = query("select id, code, name from work order by code")

    weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    date_label = f"{'Сегодня' if target_date == today else 'На'} {target_date.strftime('%d.%m.%Y')} ({weekday_names[target_date.weekday()]})"

    return templates.TemplateResponse(request, "simple.html", {
        "rows": rows, "all_works": all_works, "reason_codes": REASON_CODES,
        "date_iso": target_date.isoformat(), "date_label": date_label, "is_today": target_date == today,
    })


@app.get("/dashboard")
def home(request: Request):
    works_total = query_one("select count(*) as n from work")["n"]
    by_status = query(
        "select status, count(*) as n from work group by status order by n desc"
    )
    needs_review = query_one(
        "select count(*) as n from work where data_quality_flag='needs_review'"
    )["n"]
    unresolved = query_one("select count(*) as n from import_unresolved_cell")["n"]

    avg_pct = query_one(
        "select round(avg(fact_pct)::numeric, 1) as v, count(fact_pct) as n "
        "from work where fact_pct is not null"
    )

    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]

    today_totals = None
    if last_actual_date:
        today_totals = query_one(
            LATEST_DP_CTE + """
            select sum(planned_crew) as planned, sum(actual_crew) as actual
            from latest_dp where date=%s
            """,
            (last_actual_date,),
        )

    top_comments = query(
        LATEST_DP_CTE + """
        select comment, count(*) as n
        from latest_dp
        where comment is not null and comment <> ''
        group by comment
        order by n desc
        limit 5
        """
    )

    blockers_total = query_one("select count(*) as n from blocker")["n"]
    subcontractors_total = query_one("select count(*) as n from subcontractor")["n"]

    crit = get_criticality_data()
    evm = get_evm_data()

    return render(
        request, "home.html", "data",
        works_total=works_total, by_status=by_status,
        needs_review=needs_review, unresolved=unresolved,
        avg_pct=avg_pct, last_actual_date=last_actual_date,
        today_totals=today_totals, top_comments=top_comments,
        blockers_total=blockers_total, subcontractors_total=subcontractors_total,
        crit=crit, evm=evm,
    )


# ---------------------------------------------------------------------
# Критичные работы (ТЗ 4.3) — просрочка относительно временного baseline
# ---------------------------------------------------------------------

@app.get("/critical")
def critical(request: Request):
    crit = get_criticality_data()
    return render(request, "critical.html", "data", crit=crit)


# ---------------------------------------------------------------------
# Реестр работ
# ---------------------------------------------------------------------

@app.get("/works")
def works(request: Request, source: str = "", status: str = "", executor_type: str = "", q: str = ""):
    sql = "select code, source, location, name, unit, status, executor_type, fact_pct, fact_pct_raw, data_quality_flag from work where true"
    params = []
    if source:
        sql += " and source=%s"; params.append(source)
    if status:
        sql += " and status=%s"; params.append(status)
    if executor_type:
        sql += " and executor_type=%s"; params.append(executor_type)
    if q:
        sql += " and name ilike %s"; params.append(f"%{q}%")
    sql += " order by code"
    rows = query(sql, params)

    sources = query("select distinct source from work order by source")
    statuses = query("select distinct status from work order by status")

    return render(
        request, "works.html", "data",
        rows=rows, sources=sources, statuses=statuses,
        f_source=source, f_status=status, f_executor=executor_type, f_q=q,
    )


# ---------------------------------------------------------------------
# Справочник норм трудозатрат — ДВА источника (оба описаны в CLAUDE.md,
# разделы «Справочник норм трудозатрат» и «Второй справочник —
# СТО-ССР»). Основной — ssr_norm (СТО-ССР-2026, Spider Project,
# внутренний норматив подрядчика ООО «ССР», разделы работ совпадают со
# scope ТМ-35). Вспомогательный — gesn_norm (ГЭСН-2022, госнорма общего
# назначения, шире по охвату, но не привязана к реальной технике/
# бригадам этого подрядчика) — показывается только когда в основном
# справочнике по запросу ничего не нашлось. НИ ОДИН из двух каталогов
# не связан с конкретными работами ПТО — единица измерения у
# большинства из 163 работ Excel «комп.», не физический объём, считать
# трудозатраты нечем (см. CLAUDE.md, «Истории замен» пп. 3-4 —
# сопоставление уже пробовалось и не работает). Экран — поиск нормы +
# расчёт на объём, введённый человеком вручную, который знает реальный
# объём своей работы.
# ---------------------------------------------------------------------

NORMS_RESULT_LIMIT = 300


@app.get("/norms")
def norms(request: Request, q: str = "", section: str = ""):
    sections = query("select distinct section from ssr_norm order by section")

    ssr_rows = []
    ssr_total = 0
    if q or section:
        sql = "select section, code, name, unit, labor_hours_per_unit from ssr_norm where true"
        params = []
        if q:
            sql += " and name ilike %s"
            params.append(f"%{q}%")
        if section:
            sql += " and section=%s"
            params.append(section)
        ssr_total = query_one(f"select count(*) as n from ({sql}) t", params)["n"]
        sql += " order by section, code limit %s"
        params.append(NORMS_RESULT_LIMIT)
        ssr_rows = query(sql, params)

    gesn_rows = []
    gesn_total = 0
    if (q or section) and ssr_total == 0:
        sql = "select sbornik_title, code, name, unit, hours_per_unit from gesn_norm where true"
        params = []
        if q:
            sql += " and name ilike %s"
            params.append(f"%{q}%")
        gesn_total = query_one(f"select count(*) as n from ({sql}) t", params)["n"]
        sql += " order by sbornik_title, code limit %s"
        params.append(NORMS_RESULT_LIMIT)
        gesn_rows = query(sql, params)

    return render(
        request, "norms.html", "data",
        ssr_rows=ssr_rows, ssr_total=ssr_total, sections=sections,
        gesn_rows=gesn_rows, gesn_total=gesn_total,
        f_q=q, f_section=section, result_limit=NORMS_RESULT_LIMIT,
    )


# ---------------------------------------------------------------------
# Плановый график по 56 нормированным позициям сметы (docs/
# smeta_normalization_test_2026-08-19.md). Расчёт срока — НЕЗАВИСИМО по
# каждой позиции (если на эту работу выделить N человек с даты начала,
# когда закончится) — общий пул людей между позициями НЕ моделируется,
# это явное упрощение MVP, координатор попросил именно «по работам».
# Трудозатраты чел-час -> чел-дни через 8-часовой рабочий день (тот же
# принцип, что уже в /api/calculator).
# ---------------------------------------------------------------------

HOURS_PER_DAY = 8


@app.get("/norm-plan")
def norm_plan_page(request: Request):
    rows = query("select * from norm_plan_item order by smeta_n")
    start_raw = get_app_setting("norm_plan_start")
    start = date_cls.fromisoformat(start_raw) if start_raw else date_cls.today()

    total_hours = 0.0
    total_assigned = 0
    out_rows = []
    for r in rows:
        row = dict(r)
        total_hours += float(row["labor_hours_total"] or 0)
        working_days = forecast_date = None
        if row["assigned_people"]:
            total_assigned += 1
            remaining_days = float(row["labor_hours_total"]) / HOURS_PER_DAY
            working_days, forecast_date = compute_forecast_from_people(
                remaining_days, row["assigned_people"], start
            )
        row["working_days"] = working_days
        row["forecast_date"] = forecast_date
        out_rows.append(row)

    return render(
        request, "norm_plan.html", "data",
        rows=out_rows, start=start.isoformat(),
        total_hours=round(total_hours, 1), total_assigned=total_assigned, total_n=len(out_rows),
    )


@app.post("/norm-plan")
async def norm_plan_save(request: Request):
    form = await request.form()
    start_raw = (form.get("start") or "").strip()
    if start_raw:
        try:
            date_cls.fromisoformat(start_raw)
        except ValueError:
            start_raw = ""
    execute(
        "insert into app_setting (key, value, updated_at) values ('norm_plan_start', %s, now()) "
        "on conflict (key) do update set value=excluded.value, updated_at=now()",
        (start_raw or None,),
    )

    rows = query("select id from norm_plan_item")
    for r in rows:
        raw = (form.get(f"people_{r['id']}") or "").strip()
        people = None
        if raw:
            try:
                people = max(0, int(raw))
            except ValueError:
                people = None
        execute("update norm_plan_item set assigned_people=%s where id=%s", (people, r["id"]))

    return RedirectResponse("/norm-plan", status_code=303)


# ---------------------------------------------------------------------
# Ресурсы
# ---------------------------------------------------------------------

@app.get("/resources")
def resources(request: Request):
    rows = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual,
               sum(planned_crew) - sum(actual_crew) as deficit,
               count(*) filter (where actual_crew is not null) as works_with_fact
        from latest_dp
        group by date
        order by date
        """
    )
    resource_pool_rows = query_one("select count(*) as n from resource_pool")["n"]
    return render(request, "resources.html", "data", rows=rows, resource_pool_rows=resource_pool_rows)


# ---------------------------------------------------------------------
# Простои
# ---------------------------------------------------------------------

@app.get("/downtime")
def downtime(request: Request):
    rows = query(
        LATEST_DP_CTE + """
        select ldp.date, w.code, w.name, ldp.comment, ldp.planned_crew, ldp.actual_crew
        from latest_dp ldp
        join work w on w.id = ldp.work_id
        where ldp.comment is not null and ldp.comment <> ''
        order by ldp.date desc
        limit 200
        """
    )
    total_with_comment = query_one(
        LATEST_DP_CTE + "select count(*) as n from latest_dp where comment is not null and comment <> ''"
    )["n"]
    reason_coded = query_one(
        "select count(*) as n from daily_progress where reason_code is not null"
    )["n"]
    return render(
        request, "downtime.html", "data",
        rows=rows, total_with_comment=total_with_comment, reason_coded=reason_coded,
    )


# ---------------------------------------------------------------------
# Субподрядчики
# ---------------------------------------------------------------------

@app.get("/subcontractors")
def subcontractors(request: Request):
    registry_rows = query("select * from subcontractor order by name")
    proxy_rows = query(
        "select code, name, comment, location from work "
        "where executor_type='subcontract' order by code"
    )
    return render(
        request, "subcontractors.html", "data",
        registry_rows=registry_rows, proxy_rows=proxy_rows,
    )


# ---------------------------------------------------------------------
# Материалы и поставки
# ---------------------------------------------------------------------

@app.get("/materials")
def materials(request: Request):
    rows = query("select * from material order by name")
    return render(request, "materials.html", "data", rows=rows)


# ---------------------------------------------------------------------
# Ограничения
# ---------------------------------------------------------------------

@app.get("/blockers")
def blockers(request: Request):
    rows = query(
        "select b.*, w.code as work_code, w.name as work_name "
        "from blocker b left join work w on w.id=b.work_id "
        "order by b.created_at desc"
    )
    return render(request, "blockers.html", "data", rows=rows)


# ---------------------------------------------------------------------
# Ежедневная сводка
# ---------------------------------------------------------------------

@app.get("/daily-report")
def daily_report(request: Request, date: str = ""):
    if not date:
        last = query_one(
            "select max(date) as d from daily_progress where actual_crew is not null"
        )
        date = str(last["d"]) if last["d"] else str(date_cls.today())

    today_rows = query(
        LATEST_DP_CTE + """
        select w.code, w.name, ldp.planned_crew, ldp.actual_crew, ldp.comment, ldp.source
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s
        order by w.code
        """,
        (date,),
    )
    tomorrow_rows = query(
        LATEST_DP_CTE + """
        select w.code, w.name, ldp.planned_crew
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = (%s::date + interval '1 day')::date and ldp.planned_crew > 0
        order by w.code
        """,
        (date,),
    )
    return render(
        request, "daily_report.html", "data",
        date=date, today_rows=today_rows, tomorrow_rows=tomorrow_rows,
    )


# ---------------------------------------------------------------------
# Обоснование Исполнителя
# ---------------------------------------------------------------------

@app.get("/executor")
def executor(request: Request):
    agg = query_one(
        LATEST_DP_CTE + """
        select sum(planned_crew) as total_planned, sum(actual_crew) as total_actual,
               round(avg(planned_crew),1) as avg_planned, round(avg(actual_crew),1) as avg_actual,
               count(distinct date) as days_with_data
        from latest_dp
        """
    )
    by_source_type = query("select source, count(*) as n from work group by source order by source")
    subcontract_count = query_one(
        "select count(*) as n from work where executor_type='subcontract'"
    )["n"]
    return render(
        request, "executor.html", "data",
        agg=agg, by_source_type=by_source_type, subcontract_count=subcontract_count,
    )


# ---------------------------------------------------------------------
# Качество данных
# ---------------------------------------------------------------------

@app.get("/quality")
def quality(request: Request):
    flagged_works = query(
        "select code, name, data_quality_note from work "
        "where data_quality_flag='needs_review' order by code"
    )
    flagged_daily = query(
        "select dp.date, w.code, dp.data_quality_note from daily_progress dp "
        "join work w on w.id=dp.work_id "
        "where dp.data_quality_flag='needs_review' order by dp.date"
    )
    unresolved = query(
        "select sheet, cell_ref, work_code, issue_type, raw_payload, resolved "
        "from import_unresolved_cell order by id"
    )
    return render(
        request, "quality.html", "data",
        flagged_works=flagged_works, flagged_daily=flagged_daily, unresolved=unresolved,
    )


# ---------------------------------------------------------------------
# Веб-форма ежедневного факта (опциональный второй канал) — Цикл 2
# переработан с нуля: серверная валидация (клиентскую можно обойти),
# закрытый справочник причин простоя (ТЗ 8.8), предупреждение о дате
# сильно не "сегодня", проверка уже существующей записи за день.
# ---------------------------------------------------------------------


def validate_crew(raw, field_label, errors):
    """Целое число 0..50 или пусто. Текст/дробь/диапазон — явная ошибка,
    не тихий None (та же категория дефекта, что "16шт" в Excel — там
    валидации не было вообще, здесь обязана быть)."""
    if raw is None or raw.strip() == "":
        return None
    try:
        val = int(raw)
    except ValueError:
        errors.append(f"«{field_label}» должно быть целым числом (введено: {raw!r}).")
        return None
    if not (0 <= val <= 50):
        errors.append(f"«{field_label}» должно быть от 0 до 50 (введено: {val}).")
        return None
    return val


def validate_date(raw, errors, warnings):
    if not raw or not raw.strip():
        errors.append("«Дата» обязательна.")
        return None
    try:
        d = date_cls.fromisoformat(raw.strip())
    except ValueError:
        errors.append(f"«Дата» не распознана как дата (введено: {raw!r}).")
        return None
    delta = (d - date_cls.today()).days
    if delta < -30:
        warnings.append(f"Дата {d} — более 30 дней в прошлом. Запись сохранится, но проверьте, не опечатка ли это.")
    elif delta > 30:
        warnings.append(f"Дата {d} — более 30 дней в будущем. Запись сохранится, но проверьте, не опечатка ли это.")
    return d


@app.get("/api/existing-entry")
def api_existing_entry(work_id: int, date: str):
    row = query_one(
        "select planned_crew, actual_crew, comment, reason_code, updated_at "
        "from daily_progress where work_id=%s and date=%s and source='web_form'",
        (work_id, date),
    )
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "planned_crew": row["planned_crew"],
        "actual_crew": row["actual_crew"],
        "comment": row["comment"],
        "reason_code": row["reason_code"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@app.get("/form")
def form_get(request: Request, ok: str = "", w: str = ""):
    work_rows = query("select id, code, name from work order by code")
    warnings = w.split("||") if w else []
    return render(
        request, "form.html", "data",
        work_rows=work_rows, reason_codes=REASON_CODES,
        errors=[], warnings=warnings, ok=bool(ok), values={},
    )


@app.post("/form")
def form_post(
    request: Request,
    work_id: str = Form(""),
    date: str = Form(""),
    planned_crew: str = Form(""),
    actual_crew: str = Form(""),
    reason_code: str = Form(""),
    comment: str = Form(""),
):
    errors = []
    warnings = []

    work_row = None
    if not work_id.strip():
        errors.append("«Работа» обязательна.")
    else:
        try:
            work_row = query_one("select id, code, name from work where id=%s", (int(work_id),))
        except ValueError:
            errors.append("«Работа» указана некорректно.")
        if work_id.strip() and not work_row:
            errors.append("Выбранная работа не найдена в справочнике.")

    parsed_date = validate_date(date, errors, warnings)
    planned_val = validate_crew(planned_crew, "План людей", errors)
    actual_val = validate_crew(actual_crew, "Факт людей", errors)

    reason_val = reason_code.strip() or None
    if reason_val and reason_val not in REASON_CODE_SET:
        errors.append("Причина простоя указана некорректно.")
    if reason_val == "OTHER" and not comment.strip():
        errors.append("При причине «Иное» комментарий обязателен.")

    comment_val = comment.strip() or None
    if planned_val is None and actual_val is None and not comment_val:
        errors.append("Заполните хотя бы одно из: план людей, факт людей, комментарий — пустая запись бессмысленна.")

    if errors:
        work_rows = query("select id, code, name from work order by code")
        return render(
            request, "form.html", "data",
            work_rows=work_rows, reason_codes=REASON_CODES,
            errors=errors, warnings=warnings, ok=False,
            values={
                "work_id": work_id, "date": date, "planned_crew": planned_crew,
                "actual_crew": actual_crew, "reason_code": reason_code, "comment": comment,
            },
        )

    user_id = ensure_web_form_user()

    def _do(cur):
        cur.execute(
            """
            insert into daily_progress
                (date, work_id, planned_crew, actual_crew, reason_code, comment, source, created_by, updated_at)
            values (%s, %s, %s, %s, %s, %s, 'web_form', %s, now())
            on conflict (date, work_id, source) do update set
                planned_crew = excluded.planned_crew,
                actual_crew = excluded.actual_crew,
                reason_code = excluded.reason_code,
                comment = excluded.comment,
                updated_at = now()
            returning id
            """,
            (parsed_date, work_row["id"], planned_val, actual_val, reason_val, comment_val, user_id),
        )
        dp_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'daily_progress', %s, 'web_form_submit', "
            "jsonb_build_object('date', %s::text, 'work_id', %s, 'planned_crew', %s, "
            "'actual_crew', %s, 'reason_code', %s, 'comment', %s), 'веб-форма, второй опциональный канал')",
            (user_id, dp_id, str(parsed_date), work_row["id"], planned_val, actual_val, reason_val, comment_val),
        )
        return dp_id

    run_in_transaction(_do)
    q = "ok=1"
    if warnings:
        q += "&w=" + urllib.parse.quote("||".join(warnings))
    return RedirectResponse(url=f"/form?{q}", status_code=303)


# ---------------------------------------------------------------------
# Гант-график производства работ — главный рабочий экран (v3.0).
# Цикл 1: только просмотр. Данные — окно дат (не весь диапазон разом,
# иначе 163 x ~160 дней тяжело рендерить и незачем гонять по сети).
# ---------------------------------------------------------------------

SOURCE_LABELS = {
    "main": "Основной график",
    "iks": "Замечания ИКС",
    "rsk": "Замечания РСК",
    "aux": "Вспомогательные работы",
}


@app.get("/gantt")
def gantt_page(request: Request):
    return render(request, "gantt.html", "data")


@app.get("/api/gantt-metrics")
def api_gantt_metrics():
    """Компактная панель метрик для /gantt — Цикл 3. Те же функции, что
    на главной (get_evm_data/get_criticality_data), пересчитываются
    заново при каждом вызове — после правки в сетке фронт дёргает этот
    эндпоинт и обновляет панель без перезагрузки страницы."""
    evm = get_evm_data()
    crit = get_criticality_data()
    return {
        "weighted_pct": evm.get("weighted_pct") if evm.get("available") else None,
        "spi": evm.get("spi") if evm.get("available") else None,
        "cpi": evm.get("cpi") if evm.get("available") else None,
        "ppc_pct": evm.get("ppc_pct") if evm.get("available") else None,
        "ppc_promised": evm.get("ppc_promised") if evm.get("available") else None,
        "ppc_met": evm.get("ppc_met") if evm.get("available") else None,
        "forecast_date": crit["forecast_date"].isoformat() if crit.get("forecast_date") else None,
        "overdue_count": crit.get("overdue_count"),
        "required_crew": crit.get("required_crew"),
        "actual_crew": crit.get("actual_crew"),
        "deficit": crit.get("deficit"),
        "coverage_pct": crit.get("coverage_pct"),
    }


@app.get("/api/gantt")
def api_gantt(start: str = "", days: int = 30, active_only: str = "", location: str = ""):
    active_only = bool(active_only)
    if start:
        try:
            start_date = date_cls.fromisoformat(start)
        except ValueError:
            start_date = date_cls.today() - timedelta(days=7)
    else:
        start_date = date_cls.today() - timedelta(days=7)
    days = max(7, min(days, 90))
    end_date = start_date + timedelta(days=days - 1)

    where_extra = ""
    params = []
    if active_only:
        where_extra += " and w.status not in %s"
        params.append(tuple(DONE_STATUSES))
    if location.strip():
        where_extra += " and w.location ilike %s"
        params.append(f"%{location.strip()}%")

    works = query(
        f"""
        select w.id, w.code, w.name, w.unit, w.volume, w.fact_pct, w.status,
               w.source, w.location, w.executor_type, sc.name as subcontractor_name,
               cs.current_start, cs.current_finish
        from work w
        left join subcontractor sc on sc.id = w.subcontractor_id
        left join current_schedule cs on cs.work_id = w.id
        where true {where_extra}
        order by w.source, w.code
        """,
        params,
    )
    overdue_codes = {w["code"] for w in compute_overdue(
        query(
            """
            select w.code, w.name, w.status, bs.plan_finish
            from work w join baseline_schedule bs on bs.work_id = w.id
            where bs.plan_finish is not null
            """
        ),
        date_cls.today(),
    )}

    cells = query(
        LATEST_DP_CTE + """
        select work_id, date, planned_crew, actual_crew, reason_code, comment, source
        from latest_dp
        where date between %s and %s
        """,
        (start_date, end_date),
    )
    cell_map = {}
    for c in cells:
        cell_map.setdefault(c["work_id"], {})[c["date"].isoformat()] = {
            "p": c["planned_crew"], "a": c["actual_crew"],
            "r": c["reason_code"], "cm": _strip_source_marker(c["comment"]), "src": c["source"],
        }

    totals_rows = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual
        from latest_dp where date between %s and %s group by date
        """,
        (start_date, end_date),
    )
    totals = {t["date"].isoformat(): {"p": t["planned"] or 0, "a": t["actual"] or 0} for t in totals_rows}

    day_blockers_rows = query(
        "select date(created_at) as d, blocker_type, count(*) as n from blocker "
        "where work_id is null and created_at::date between %s and %s "
        "group by date(created_at), blocker_type",
        (start_date, end_date),
    )
    day_blockers = {}
    for r in day_blockers_rows:
        day_blockers.setdefault(r["d"].isoformat(), []).append(r["blocker_type"])

    groups = {}
    for w in works:
        g = groups.setdefault(w["source"], {"key": w["source"], "label": SOURCE_LABELS.get(w["source"], w["source"]), "works": []})
        g["works"].append({
            "id": w["id"], "code": w["code"], "name": w["name"], "unit": w["unit"],
            "volume": float(w["volume"]) if w["volume"] is not None else None,
            "fact_pct": float(w["fact_pct"]) if w["fact_pct"] is not None else None,
            "status": w["status"], "location": w["location"], "executor_type": w["executor_type"],
            "critical": w["code"] in overdue_codes,
            "subcontractor_name": w["subcontractor_name"],
            "current_start": w["current_start"].isoformat() if w["current_start"] else None,
            "current_finish": w["current_finish"].isoformat() if w["current_finish"] else None,
            "cells": cell_map.get(w["id"], {}),
        })

    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(days)]

    return {
        "start": start_date.isoformat(), "end": end_date.isoformat(), "days": days,
        "today": date_cls.today().isoformat(),
        "dates": dates,
        "groups": list(groups.values()),
        "totals": totals,
        "day_blockers": day_blockers,
        "reason_codes": REASON_CODES,
    }


# ---------------------------------------------------------------------
# Гант-график — Цикл 2: редактирование прямо в сетке. Все мутации через
# веб-форму пишутся source='web_form' в daily_progress (тот же канал и
# то же правило конфликта, что и /form), с audit_log на каждое действие.
# ---------------------------------------------------------------------

@app.post("/api/gantt/cell")
def api_gantt_cell_save(
    work_id: int = Form(...), date: str = Form(""),
    planned_crew: str = Form(""), actual_crew: str = Form(""),
    reason_code: str = Form(""), comment: str = Form(""),
):
    errors = []
    planned_val = validate_crew(planned_crew, "План людей", errors)
    actual_val = validate_crew(actual_crew, "Факт людей", errors)
    try:
        d = date_cls.fromisoformat(date)
    except ValueError:
        errors.append("Некорректная дата.")
        d = None
    reason_val = reason_code.strip() or None
    if reason_val and reason_val not in REASON_CODE_SET:
        errors.append("Причина простоя указана некорректно.")
    if reason_val == "OTHER" and not comment.strip():
        errors.append("При причине «Иное» комментарий обязателен.")
    comment_val = comment.strip() or None
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = ensure_web_form_user()

    def _do(cur):
        cur.execute(
            """
            insert into daily_progress
                (date, work_id, planned_crew, actual_crew, reason_code, comment, source, created_by, updated_at)
            values (%s, %s, %s, %s, %s, %s, 'web_form', %s, now())
            on conflict (date, work_id, source) do update set
                planned_crew = excluded.planned_crew, actual_crew = excluded.actual_crew,
                reason_code = excluded.reason_code, comment = excluded.comment, updated_at = now()
            returning id
            """,
            (d, work_id, planned_val, actual_val, reason_val, comment_val, user_id),
        )
        dp_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'daily_progress', %s, 'gantt_cell_edit', "
            "jsonb_build_object('date', %s::text, 'work_id', %s, 'planned_crew', %s, "
            "'actual_crew', %s, 'reason_code', %s, 'comment', %s), 'правка в графике')",
            (user_id, dp_id, str(d), work_id, planned_val, actual_val, reason_val, comment_val),
        )

    run_in_transaction(_do)
    return {"ok": True}


@app.post("/api/gantt/schedule")
def api_gantt_schedule_save(
    work_id: int = Form(...), current_start: str = Form(""), current_finish: str = Form(""),
):
    errors = []
    start_d = finish_d = None
    if current_start.strip():
        try:
            start_d = date_cls.fromisoformat(current_start.strip())
        except ValueError:
            errors.append("Некорректная дата начала.")
    if current_finish.strip():
        try:
            finish_d = date_cls.fromisoformat(current_finish.strip())
        except ValueError:
            errors.append("Некорректная дата окончания.")
    if start_d and finish_d and finish_d < start_d:
        errors.append("Дата окончания раньше даты начала.")
    if not start_d and not finish_d:
        errors.append("Укажите хотя бы одну дату.")
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = ensure_web_form_user()

    def _do(cur):
        # Одна актуальная строка current_schedule на работу — не журнал версий.
        cur.execute("delete from current_schedule where work_id=%s", (work_id,))
        cur.execute(
            "insert into current_schedule (work_id, current_start, current_finish, updated_by, reason) "
            "values (%s, %s, %s, %s, %s) returning id",
            (work_id, start_d, finish_d, user_id, "изменено через график (веб)"),
        )
        cs_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'current_schedule', %s, 'gantt_schedule_edit', "
            "jsonb_build_object('work_id', %s, 'current_start', %s, 'current_finish', %s), "
            "'сдвиг сроков через график')",
            (user_id, cs_id, work_id, str(start_d) if start_d else None, str(finish_d) if finish_d else None),
        )

    run_in_transaction(_do)
    return {"ok": True}


@app.post("/api/gantt/work")
def api_gantt_new_work(
    source: str = Form(""), name: str = Form(""), unit: str = Form(""), location: str = Form(""),
):
    errors = []
    if source not in SOURCE_LABELS:
        errors.append("Некорректный источник.")
    if not name.strip():
        errors.append("Наименование обязательно.")
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = ensure_web_form_user()
    prefix = {"main": "MAIN", "iks": "IKS", "rsk": "RSK", "aux": "AUX"}[source]

    def _do(cur):
        cur.execute("select code from work where source=%s order by code desc limit 1", (source,))
        row = cur.fetchone()
        next_seq = 1
        if row:
            m = re.search(r"-(\d+)$", row["code"])
            if m:
                next_seq = int(m.group(1)) + 1
        code = f"TM35-{prefix}-{next_seq:03d}"
        cur.execute(
            "insert into work (code, source, name, unit, location, status, executor_type) "
            "values (%s, %s, %s, %s, %s, 'not_started', 'own_forces') returning id",
            (code, source, name.strip(), unit.strip() or None, location.strip() or None),
        )
        wid = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'work', %s, 'gantt_new_work', "
            "jsonb_build_object('code', %s, 'name', %s), 'добавлено через график')",
            (user_id, wid, code, name.strip()),
        )
        return code

    code = run_in_transaction(_do)
    return {"ok": True, "code": code}


@app.post("/api/gantt/subcontractor")
def api_gantt_subcontractor(work_id: int = Form(...), name: str = Form("")):
    if not name.strip():
        return JSONResponse({"ok": False, "errors": ["Название субподрядчика обязательно."]}, status_code=400)

    user_id = ensure_web_form_user()

    def _do(cur):
        cur.execute("select id from subcontractor where name=%s", (name.strip(),))
        row = cur.fetchone()
        if row:
            sub_id = row["id"]
        else:
            cur.execute("insert into subcontractor (name) values (%s) returning id", (name.strip(),))
            sub_id = cur.fetchone()["id"]
        cur.execute(
            "update work set subcontractor_id=%s, executor_type='subcontract', updated_at=now() where id=%s",
            (sub_id, work_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'work', %s, 'gantt_assign_subcontractor', "
            "jsonb_build_object('subcontractor', %s), 'назначено через график')",
            (user_id, work_id, name.strip()),
        )

    run_in_transaction(_do)
    return {"ok": True}


# ---------------------------------------------------------------------
# "Данные" — служебный раздел (реинжиниринг v3, финал): все реестры и
# справочники, которые раньше были 12 отдельными пунктами верхнего меню.
# Ничего не спрятано — каждая карточка честно показывает, сколько в ней
# реально есть строк, включая полностью пустые реестры (subcontractor/
# material — задача #29, не заполнялись из меток Excel).
# ---------------------------------------------------------------------

@app.get("/data")
def data_hub(request: Request):
    counts = {
        "dashboard": query_one("select count(*) as n from work")["n"],
        "critical": query_one(
            "select count(*) as n from baseline_schedule where plan_finish < current_date and confidence in ('high','medium')"
        )["n"],
        "works": query_one("select count(*) as n from work")["n"],
        "resources": query_one("select count(distinct date) as n from daily_progress")["n"],
        "downtime": query_one(
            "select count(*) as n from daily_progress where comment is not null and comment <> ''"
        )["n"],
        "subcontractors": query_one("select count(*) as n from subcontractor")["n"],
        "materials": query_one("select count(*) as n from material")["n"],
        "blockers": query_one("select count(*) as n from blocker where status='active'")["n"],
        "executor": query_one("select count(*) as n from work where executor_type='subcontract'")["n"],
        "quality": query_one(
            "select count(*) as n from work where data_quality_flag='needs_review'"
        )["n"],
        "gantt": query_one("select count(*) as n from work")["n"],
        "ssr_norms": query_one("select count(*) as n from ssr_norm")["n"],
        "norm_plan": query_one("select count(*) as n from norm_plan_item")["n"],
    }
    return render(request, "data.html", "data", counts=counts)


@app.get("/healthz")
def healthz():
    query_one("select 1 as ok")
    return {"status": "ok"}
