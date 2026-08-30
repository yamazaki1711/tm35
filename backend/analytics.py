"""
Аналитический слой ТМ-35 — расчёт критичности отставания, прогноза
завершения и ресурсного дефицита. Чистые функции над списками словарей
(без обращения к БД) — чтобы их можно было проверить на известных
значениях (см. блок self-test внизу файла), не поднимая Postgres.

Формулы намеренно простые и полностью объяснимые (см. docs/GAP_ANALYSIS.md
и docs/REENGINEERING_LOG.md, Цикл 1) — там же обоснование, почему не
используются формулы ТЗ 9.4 (H_remaining, N_required): они требуют норм
человеко-часов на единицу объёма, которых нет ни в Excel, ни в схеме.
Baseline — временный (из "Месяц выполнения работ"), это тоже явно
показывается на дашборде, не скрывается.
"""
import math
from datetime import timedelta

DONE_STATUSES = {"done_physically", "submitted", "accepted", "closed", "cancelled"}


def compute_overdue(works, today):
    """
    works: [{code, name, plan_finish: date|None, status}, ...]
    today: date
    Возвращает работы, у которых temp-baseline срок уже прошёл, а статус
    не "выполнена/принята/закрыта/отменена" — отсортировано по убыванию
    просрочки. Это и есть "критические работы" ТЗ 4.3 в первом приближении.
    """
    result = []
    for w in works:
        pf = w.get("plan_finish")
        if pf is None or w.get("status") in DONE_STATUSES:
            continue
        if pf < today:
            result.append({**w, "lag_days": (today - pf).days})
    return sorted(result, key=lambda x: -x["lag_days"])


def compute_project_forecast(active_baseline_finishes, overdue_lag_days):
    """
    active_baseline_finishes: [date, ...] — baseline-срок по ещё не
        завершённым работам, у которых он известен.
    overdue_lag_days: [int, ...] — просрочка (в днях) уже сейчас
        просроченных работ (выход compute_overdue).

    forecast = самый поздний срок по временному baseline среди
    незавершённых работ + средняя текущая просрочка (если есть
    просроченные работы — иначе проект по имеющимся данным укладывается
    в свой временный график, и это тоже нужно явно показать).

    Возвращает (forecast_date, avg_lag_days, baseline_date) или (None, 0,
    None), если baseline вообще отсутствует.
    """
    if not active_baseline_finishes:
        return None, 0, None
    baseline_date = max(active_baseline_finishes)
    if overdue_lag_days:
        avg_lag = round(sum(overdue_lag_days) / len(overdue_lag_days))
    else:
        avg_lag = 0
    return baseline_date + timedelta(days=avg_lag), avg_lag, baseline_date


def compute_work_weight(trudoemkost, baseline_crew):
    """
    Вес работы для взвешенного прогресса/EVM. Приоритет источника:
    1) trudoemkost — Σ planned_crew по календарной матрице Excel
       (человеко-дни) — подтверждено на реальных данных сверкой с
       "Кол-во чел." (см. docs/REENGINEERING_LOG.md, Цикл EVM): для
       работ с календарными данными Σ План/дни ≈ заявленная численность.
    2) baseline_crew — "Кол-во чел." из Excel, если календарных данных
       по работе нет вообще (не участвует в План/Факт матрице).
    3) 1 (условная единица) — если нет вообще никакого сигнала. НЕ
       выбрасываем работу из знаменателя (это и была причина исходного
       бага "88.7% по 64 из 163" — 99 работ молча выпадали).
    Возвращает (вес, источник) — источник обязательно показывается на
    дашборде, чтобы не выдавать оценку за точный факт.
    """
    if trudoemkost is not None and trudoemkost > 0:
        return trudoemkost, "trudoemkost"
    if baseline_crew is not None and baseline_crew > 0:
        return baseline_crew, "baseline_crew"
    return 1, "flat"


def compute_weighted_progress(works):
    """
    works: [{code, weight, fact_pct: 0..100|None}, ...]
    Неизвестный % (fact_pct=None) считается 0% — консервативно и явно
    (домен: "непринятая работа считается 0%, это надо явно показать, не
    замалчивать" — координатор, доменный разбор п.2.1). Возвращает
    (взвешенный_процент, суммарный_вес, кол-во_работ_с_неизвестным_%).
    """
    total_weight = sum(float(w["weight"]) for w in works)
    if total_weight == 0:
        return None, 0, 0
    # fact_pct из Postgres приходит как Decimal — приводим к float явно,
    # иначе sum() падает на смеси Decimal/float (None -> 0 даёт float).
    earned = sum(float(w["weight"]) * float(w["fact_pct"] or 0) / 100 for w in works)
    unknown = sum(1 for w in works if w["fact_pct"] is None)
    return round(100 * earned / total_weight, 1), total_weight, unknown


def compute_evm(bcws, acwp, bcwp):
    """SPI = BCWP/BCWS (график), трудовой CPI = BCWP/ACWP (эффективность
    использования людей). None, если делитель 0 — не делим на ноль и не
    подставляем произвольное число."""
    spi = round(bcwp / bcws, 2) if bcws else None
    cpi = round(bcwp / acwp, 2) if acwp else None
    return spi, cpi


def compute_ppc(daily_rows):
    """
    daily_rows: [{planned_crew, actual_crew}, ...] — уже свёрнутые
    (latest-wins) строки daily_progress за нужный период.
    PPC = обещано и выполнено (факт >= план, план>0) / обещано (план>0).
    ТЗ ориентир (LPS) — выше 80% означает надёжное планирование.
    Возвращает (ppc_pct, promised, met) или (None, 0, 0), если обещаний
    в периоде не было.
    """
    promised = [r for r in daily_rows if (r.get("planned_crew") or 0) > 0]
    if not promised:
        return None, 0, 0
    met = sum(1 for r in promised if (r.get("actual_crew") or 0) >= r["planned_crew"])
    return round(100 * met / len(promised), 1), len(promised), met


def count_working_days(start_exclusive, end_inclusive):
    """Число рабочих дней (Пн-Пт) в (start, end] — start не считается,
    выходные не считаются рабочими. Не учитывает праздники (не знаем их
    список) — консервативно занижает требуемый темп чуть меньше реального."""
    if end_inclusive <= start_exclusive:
        return 0
    n = 0
    d = start_exclusive + timedelta(days=1)
    while d <= end_inclusive:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def compute_required_people(remaining_effort_days, today, target_date):
    """
    ТЗ 4.5/9.4.2, исправленная формула (docs/REENGINEERING_LOG.md,
    находка координатора: сумма по активным работам — не требуемая
    численность, а бессмысленно завышенное число, 272 при 12 факт.).
    required = remaining_effort_days / working_days(today..target).
    remaining_effort_days — ТОЛЬКО по работам с известной трудоёмкостью
    (не выдумываем для работ без данных — см. get_evm_data.known_work_count).
    Возвращает (requred_people:int|None, working_days:int).
    None, если срок уже прошёл/сегодня или трудоёмкость неизвестна.
    """
    working_days = count_working_days(today, target_date)
    if working_days <= 0 or remaining_effort_days is None:
        return None, working_days
    return math.ceil(remaining_effort_days / working_days), working_days


def compute_forecast_from_people(remaining_effort_days, available_people_per_day, today):
    """
    Обратная задача: заданная численность -> дата завершения (рабочие дни,
    выходные пропускаются). Возвращает (working_days_needed, forecast_date)
    или (None, None), если численность <= 0 или трудоёмкость неизвестна.
    """
    if not available_people_per_day or available_people_per_day <= 0 or remaining_effort_days is None:
        return None, None
    working_days_needed = math.ceil(remaining_effort_days / available_people_per_day)
    d = today
    counted = 0
    while counted < working_days_needed:
        d += timedelta(days=1)
        if d.weekday() < 5:
            counted += 1
    return working_days_needed, d


def compute_forecast_by_pace(remaining_effort_days, recent_daily_actuals, today):
    """
    Вторая, независимая оценка прогноза (докладная координатора "что делают
    отраслевые системы", Цикл 1 экрана "Успеваем?") — НЕ заменяет
    compute_project_forecast (baseline + средняя просрочка), а дополняет:
    прогноз = остаток трудоёмкости ÷ средний фактический темп (чел-дней/
    рабочий день) за последние N дней с фактом. Обе оценки показываются
    рядом с явной методикой — не сводятся в одно число молча.

    recent_daily_actuals: [float,...] — суммарная фактическая численность
    по каждому из последних N рабочих дней С ДАННЫМИ (не календарных дней
    подряд — если данных за день нет, день просто не входит в список).

    Возвращает (forecast_date, avg_daily_pace, working_days_needed).
    avg_daily_pace — None, если данных нет вообще. forecast_date — None,
    если трудоёмкость неизвестна или темп <= 0 (буксуем — дату считать
    нельзя, это тоже сигнал, не подставляем произвольное число).
    """
    if not recent_daily_actuals:
        return None, None, None
    avg_pace = round(sum(recent_daily_actuals) / len(recent_daily_actuals), 2)
    if remaining_effort_days is None or avg_pace <= 0:
        return None, avg_pace, None
    working_days_needed = math.ceil(remaining_effort_days / avg_pace)
    d = today
    counted = 0
    while counted < working_days_needed:
        d += timedelta(days=1)
        if d.weekday() < 5:
            counted += 1
    return d, avg_pace, working_days_needed


def compute_resource_deficit(required_crew_total, actual_crew_total):
    """
    required_crew_total: сумма "Кол-во чел." (заявлено по Excel) по
        активным (не завершённым) работам — сколько бригад нужно, если
        считать все активные работы одновременно (упрощение: реально
        часть работ идёт последовательно, не параллельно — это не точный
        план ресурса, а верхняя оценка потребности, что явно поясняется
        на дашборде).
    actual_crew_total: фактически вышло людей на последний день с данными.

    Возвращает (deficit, coverage_pct) — coverage_pct = какая доля
    заявленной потребности реально обеспечена ресурсом.
    """
    if required_crew_total is None or actual_crew_total is None:
        return None, None
    deficit = required_crew_total - actual_crew_total
    coverage_pct = (
        round(100 * actual_crew_total / required_crew_total, 1)
        if required_crew_total > 0
        else None
    )
    return deficit, coverage_pct


# --------------------------- self-test --------------------------------
if __name__ == "__main__":
    from datetime import date

    today = date(2026, 8, 15)

    works = [
        {"code": "A", "name": "Работа A", "plan_finish": date(2026, 7, 31), "status": "in_progress"},
        {"code": "B", "name": "Работа B", "plan_finish": date(2026, 8, 31), "status": "not_started"},
        {"code": "C", "name": "Работа C", "plan_finish": date(2026, 7, 1), "status": "done_physically"},
        {"code": "D", "name": "Работа D", "plan_finish": None, "status": "not_started"},
    ]
    overdue = compute_overdue(works, today)
    assert [w["code"] for w in overdue] == ["A"], overdue
    assert overdue[0]["lag_days"] == 15, overdue  # 31 июля -> 15 августа = 15 дней
    print("compute_overdue: OK", overdue)

    forecast, avg_lag, baseline = compute_project_forecast(
        [date(2026, 7, 31), date(2026, 8, 31)], [15]
    )
    assert baseline == date(2026, 8, 31)
    assert avg_lag == 15
    assert forecast == date(2026, 9, 15)
    print("compute_project_forecast (с просрочкой): OK", forecast, avg_lag, baseline)

    forecast2, avg_lag2, baseline2 = compute_project_forecast(
        [date(2026, 8, 31)], []
    )
    assert avg_lag2 == 0 and forecast2 == baseline2 == date(2026, 8, 31)
    print("compute_project_forecast (без просрочки): OK", forecast2)

    forecast3, avg_lag3, baseline3 = compute_project_forecast([], [])
    assert forecast3 is None and baseline3 is None
    print("compute_project_forecast (нет baseline): OK")

    deficit, coverage = compute_resource_deficit(30, 12)
    assert deficit == 18 and coverage == 40.0
    print("compute_resource_deficit: OK", deficit, coverage)

    deficit0, coverage0 = compute_resource_deficit(0, 5)
    assert deficit0 == -5 and coverage0 is None
    print("compute_resource_deficit (required=0): OK")

    # --- EVM/PPC (доменный разбор v2.0) ---
    w, src = compute_work_weight(30, 5)
    assert w == 30 and src == "trudoemkost"
    w2, src2 = compute_work_weight(None, 5)
    assert w2 == 5 and src2 == "baseline_crew"
    w3, src3 = compute_work_weight(None, None)
    assert w3 == 1 and src3 == "flat"
    print("compute_work_weight: OK")

    works = [
        {"code": "A", "weight": 30, "fact_pct": 100},
        {"code": "B", "weight": 10, "fact_pct": 50},
        {"code": "C", "weight": 1, "fact_pct": None},  # неизвестно -> 0%
    ]
    wp, total_w, unknown = compute_weighted_progress(works)
    # (30*1.0 + 10*0.5 + 1*0) / 41 = 35/41 = 85.4%
    assert total_w == 41 and unknown == 1
    assert abs(wp - 85.4) < 0.05, wp
    print("compute_weighted_progress: OK", wp)

    spi, cpi = compute_evm(bcws=100, acwp=80, bcwp=90)
    assert spi == 0.9 and cpi == 1.12, (spi, cpi)  # 90/100=0.9, 90/80=1.125 -> round-half-even 1.12
    spi0, cpi0 = compute_evm(bcws=0, acwp=0, bcwp=0)
    assert spi0 is None and cpi0 is None
    print("compute_evm: OK", spi, cpi)

    ppc, promised, met = compute_ppc([
        {"planned_crew": 5, "actual_crew": 5},
        {"planned_crew": 3, "actual_crew": 1},
        {"planned_crew": 0, "actual_crew": 2},  # не обещание — не считается
        {"planned_crew": 4, "actual_crew": 4},
    ])
    assert promised == 3 and met == 2, (promised, met)
    assert abs(ppc - 66.7) < 0.05, ppc
    print("compute_ppc: OK", ppc, promised, met)

    ppc_empty, _, _ = compute_ppc([{"planned_crew": 0, "actual_crew": 0}])
    assert ppc_empty is None
    print("compute_ppc (нет обещаний): OK")

    # --- Исправленная формула требуемой численности (не сумма по активным) ---
    mon = date(2026, 8, 17)  # понедельник
    fri = date(2026, 8, 21)  # пятница той же недели
    wd = count_working_days(mon, fri)
    assert wd == 4, wd  # вт,ср,чт,пт — сб/вс не входят, если бы были в диапазоне
    print("count_working_days: OK", wd)

    req, wd2 = compute_required_people(100, mon, fri)
    assert wd2 == 4 and req == 25, (req, wd2)  # ceil(100/4)=25
    print("compute_required_people: OK", req)

    req_none, wd0 = compute_required_people(100, mon, mon)
    assert req_none is None and wd0 == 0
    print("compute_required_people (срок сегодня): OK")

    needed_days, forecast = compute_forecast_from_people(40, 10, mon)
    assert needed_days == 4 and forecast == fri, (needed_days, forecast)
    print("compute_forecast_from_people: OK", needed_days, forecast)

    nd0, f0 = compute_forecast_from_people(40, 0, mon)
    assert nd0 is None and f0 is None
    print("compute_forecast_from_people (0 человек): OK")

    # --- Прогноз по фактическому темпу (докладная "отраслевые системы") ---
    fdate, pace, wdn = compute_forecast_by_pace(100, [10, 12, 8, 10], mon)
    # avg_pace = (10+12+8+10)/4 = 10; ceil(100/10) = 10 рабочих дней от mon
    assert pace == 10.0 and wdn == 10
    print("compute_forecast_by_pace: OK", fdate, pace, wdn)

    fdate0, pace0, wdn0 = compute_forecast_by_pace(100, [], mon)
    assert fdate0 is None and pace0 is None and wdn0 is None
    print("compute_forecast_by_pace (нет данных по темпу): OK")

    fdate1, pace1, wdn1 = compute_forecast_by_pace(100, [0, 0], mon)
    assert fdate1 is None and pace1 == 0.0 and wdn1 is None
    print("compute_forecast_by_pace (нулевой темп — буксуем): OK")

    print("\nВсе самотесты analytics.py прошли.")
