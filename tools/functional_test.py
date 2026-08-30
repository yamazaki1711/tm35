#!/usr/bin/env python3
"""
Функциональная проверка ТМ-35 — проходит систему как пользователь:
выполняет реальные действия (клик, ввод, сохранение) и проверяет
результат, а не просто наличие текста на странице (для этого —
tools/ui_audit.py). Нашла и подтвердила исправление реального бага
20.08.2026 (см. docs/UI_AUDIT.md, раздел «Сквозная проверка»):
календарь (`dm-picker`) закрывался сразу при клике на «следующий/
предыдущий месяц» — навигация была невозможна нигде в системе, где
есть поле даты.

Каждый сценарий — отдельная функция, возвращает (ok: bool, detail: str).
Сценарии, которые пишут в БД (Гантт, форма факта, директивный срок),
подчищают за собой сами — прогон повторяем, не копит тестовые записи.

Прогонять после любой правки интерфейса, до отчёта о выполнении.
Использование:
    python3 tools/functional_test.py [--base-url URL]
"""
import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

RESULTS = []


def scenario(name):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                ok, detail = fn(*a, **kw)
            except Exception as e:
                ok, detail = False, f"исключение: {e!r}"
            RESULTS.append((name, ok, detail))
            mark = "OK  " if ok else "FAIL"
            print(f"[{mark}] {name} — {detail}")
            return ok
        return wrapper
    return deco


# ---------------------------------------------------------------------
# Ввод факта — «Иное» открывает комментарий, сохраняется, валидация
# ---------------------------------------------------------------------

@scenario("Простой ввод факта: «Иное» открывает поле комментария")
def test_simple_other_opens_comment(page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    page.wait_for_timeout(300)
    card = page.locator(".work-card").first
    if card.count() == 0:
        return False, "нет ни одной карточки работы на сегодня — сценарий не проверить"
    wid = card.get_attribute("id").replace("card-", "")
    wrap_before = page.locator(f"#reason-comment-wrap-{wid}").is_visible()
    page.locator(f"#reason-{wid}").select_option("OTHER")
    page.wait_for_timeout(150)
    wrap_after = page.locator(f"#reason-comment-wrap-{wid}").is_visible()
    page.locator(f"#reason-{wid}").select_option("")
    if wrap_before:
        return False, f"поле комментария было видно ДО выбора «Иное» (work_id={wid})"
    if not wrap_after:
        return False, f"поле комментария НЕ появилось после выбора «Иное» (work_id={wid})"
    return True, f"work_id={wid}, поле появляется/скрывается корректно"


@scenario("Простой ввод факта: «Иное» без комментария не сохраняется (клиентская валидация)")
def test_simple_other_requires_comment(page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    page.wait_for_timeout(300)
    card = page.locator(".work-card").first
    if card.count() == 0:
        return False, "нет карточек — не проверить"
    wid = card.get_attribute("id").replace("card-", "")
    page.locator(f"#reason-{wid}").select_option("OTHER")
    page.wait_for_timeout(150)
    page.fill(f"#reason-comment-{wid}", "")
    page.click(f"#card-{wid} .save-btn")
    page.wait_for_timeout(300)
    err = page.locator(f"#err-{wid}")
    ok_visible = page.locator(f"#ok-{wid}").is_visible()
    page.locator(f"#reason-{wid}").select_option("")
    if ok_visible:
        return False, "запись сохранилась БЕЗ обязательного комментария"
    if not err.is_visible():
        return False, "ошибка не показана при пустом комментарии"
    return True, err.inner_text()


@scenario("Простой ввод факта: «Иное» с комментарием сохраняется и переживает перезагрузку")
def test_simple_other_persists(page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    page.wait_for_timeout(300)
    card = page.locator(".work-card").first
    if card.count() == 0:
        return False, "нет карточек — не проверить"
    wid = card.get_attribute("id").replace("card-", "")
    marker = f"functional_test {int(time.time())}"
    page.locator(f"#reason-{wid}").select_option("OTHER")
    page.wait_for_timeout(150)
    page.fill(f"#reason-comment-{wid}", marker)
    page.click(f"#card-{wid} .save-btn")
    page.wait_for_timeout(700)
    saved = page.locator(f"#ok-{wid}").is_visible()
    if not saved:
        return False, "сохранение не подтвердилось (нет ✓)"

    page.goto(base_url + "/", wait_until="networkidle")
    page.wait_for_timeout(300)
    reason_val = page.locator(f"#reason-{wid}").input_value()
    wrap_visible = page.locator(f"#reason-comment-wrap-{wid}").is_visible()
    comment_val = page.locator(f"#reason-comment-{wid}").input_value()

    # очистка — возвращаем причину/комментарий в пустое состояние. Порядок
    # важен: сначала очистить textarea, ПОТОМ сбросить select — сброс select
    # прячет обёртку (toggleReasonComment), и .fill() на уже скрытом поле
    # падает по таймауту (Playwright не пишет в невидимый элемент).
    page.fill(f"#reason-comment-{wid}", "")
    page.locator(f"#reason-{wid}").select_option("")
    page.click(f"#card-{wid} .save-btn")
    page.wait_for_timeout(700)

    if reason_val != "OTHER" or not wrap_visible or marker not in comment_val:
        return False, f"после перезагрузки: reason={reason_val!r} wrap_visible={wrap_visible} comment={comment_val!r}"
    return True, "значение и открытое поле комментария пережили перезагрузку"


@scenario("Простой ввод факта: отрицательное/>50 отклоняется с понятной ошибкой, не падает")
def test_simple_invalid_crew(page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    page.wait_for_timeout(300)
    card = page.locator(".work-card").first
    if card.count() == 0:
        return False, "нет карточек — не проверить"
    wid = card.get_attribute("id").replace("card-", "")
    msgs = []
    for bad in ("-5", "999"):
        page.fill(f"#actual-{wid}", bad)
        page.click(f"#card-{wid} .save-btn")
        page.wait_for_timeout(400)
        err = page.locator(f"#err-{wid}")
        if not err.is_visible():
            return False, f"значение {bad!r} не отклонено (нет сообщения об ошибке)"
        msgs.append(err.inner_text())
    page.fill(f"#actual-{wid}", "")
    return True, " | ".join(msgs)


# ---------------------------------------------------------------------
# Гантт — фильтры, группы, ячейки, метрики, закреплённая колонка
# ---------------------------------------------------------------------

@scenario("Гантт: ← пред./след. → меняют диапазон дат")
def test_gantt_date_nav(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    before = page.locator("#gantt-status").inner_text()
    page.click("#btn-next")
    page.wait_for_timeout(900)
    after_next = page.locator("#gantt-status").inner_text()
    page.click("#btn-today")
    page.wait_for_timeout(900)
    after_today = page.locator("#gantt-status").inner_text()
    if before == after_next:
        return False, f"«след. →» не изменил диапазон ({before!r})"
    if after_today != before:
        return False, f"«Сегодня» не вернул исходный диапазон: {before!r} != {after_today!r}"
    return True, f"{before} → {after_next} → (Сегодня) → {after_today}"


@scenario("Гантт: фильтр по участку сужает список работ")
def test_gantt_location_filter(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    rows_before = page.locator("table.gantt tr").count()
    page.fill("#inp-location", "УТ1")
    page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    rows_after = page.locator("table.gantt tr").count()
    page.fill("#inp-location", "")
    page.click("#btn-apply")
    page.wait_for_timeout(700)
    if rows_after >= rows_before:
        return False, f"фильтр не сузил список: {rows_before} -> {rows_after}"
    return True, f"{rows_before} -> {rows_after} строк"


@scenario("Гантт: «только активные» сужает список")
def test_gantt_active_only(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    rows_before = page.locator("table.gantt tr").count()
    page.check("#chk-active")
    page.wait_for_timeout(900)
    rows_after = page.locator("table.gantt tr").count()
    page.uncheck("#chk-active")
    page.wait_for_timeout(700)
    if rows_after >= rows_before:
        return False, f"галка не сузила список: {rows_before} -> {rows_after}"
    return True, f"{rows_before} -> {rows_after} строк"


@scenario("Гантт: «Применить» применяет период+участок+галку разом")
def test_gantt_apply_combo(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    page.select_option("#sel-days", "14")
    page.fill("#inp-location", "УУСА1")
    page.check("#chk-active")
    page.click("#btn-apply")
    page.wait_for_timeout(900)
    status = page.locator("#gantt-status").inner_text()
    rows = page.locator("table.gantt tr").count()
    # сброс
    page.uncheck("#chk-active")
    page.fill("#inp-location", "")
    page.select_option("#sel-days", "30")
    page.click("#btn-apply")
    page.wait_for_timeout(700)
    ok = "…" in status and rows > 0
    return ok, f"диапазон={status}, строк={rows}"


@scenario("Гантт: группа сворачивается и разворачивается")
def test_gantt_group_toggle(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    group = page.locator(".group-row").first
    first_row_visible_before = page.locator(".work-row-of-group").first.is_visible()
    group.locator(".grp-label").click()
    page.wait_for_timeout(250)
    collapsed = not page.locator(".work-row-of-group").first.is_visible()
    group.locator(".grp-label").click()
    page.wait_for_timeout(250)
    expanded_again = page.locator(".work-row-of-group").first.is_visible()
    if not (first_row_visible_before and collapsed and expanded_again):
        return False, f"before={first_row_visible_before} collapsed={collapsed} expanded_again={expanded_again}"
    return True, "свернуть/развернуть работает"


@scenario("Гантт: правка ячейки сохраняется и видна после обновления")
def test_gantt_cell_edit(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    cell = page.locator(".daycol[data-work]").nth(60)
    work_id = cell.get_attribute("data-work")
    date = cell.get_attribute("data-date")
    cell.click()
    page.wait_for_timeout(250)
    if not page.locator("#modal-cell").evaluate('el => el.classList.contains("open")'):
        return False, "модалка ячейки не открылась"
    page.fill("#mc-planned", "2")
    page.fill("#mc-actual", "2")
    page.click("#mc-save")
    page.wait_for_timeout(1500)
    toast_ok = page.locator("#toast").is_visible()
    new_cell = page.locator(f'td[data-work="{work_id}"][data-date="{date}"]')
    text_after = new_cell.inner_text()

    # очистка через API напрямую (быстрее, чем через UI второй раз)
    page.evaluate(
        """
        ([workId, date]) => fetch('/api/gantt/cell', {
          method: 'POST',
          body: new URLSearchParams({work_id: workId, date: date, planned_crew: '', actual_crew: '', reason_code: '', comment: ''})
        })
        """,
        [work_id, date],
    )
    page.wait_for_timeout(300)

    if not toast_ok or "2/2" not in text_after:
        return False, f"toast={toast_ok}, ячейка после сохранения={text_after!r}"
    return True, f"work_id={work_id} date={date} -> {text_after}"


@scenario("Гантт: закреплённый левый блок не уезжает при горизонтальной прокрутке")
def test_gantt_sticky_column(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    x_before = page.locator(".col-code").first.bounding_box()["x"]
    page.locator("#gantt-scroll").evaluate("el => el.scrollLeft = 600")
    page.wait_for_timeout(250)
    x_after = page.locator(".col-code").first.bounding_box()["x"]
    if abs(x_before - x_after) > 2:
        return False, f"колонка сдвинулась: {x_before} -> {x_after}"
    return True, f"x={x_before} до и после прокрутки на 600px"


@scenario("Гантт: модалка «+добавить работу» открывается, поля заполняются, «Отмена» закрывает")
def test_gantt_add_work_modal(page, base_url):
    # Полный путь сохранения (создаёт запись в work) проверен вручную
    # 20.08.2026 — сработал (toast «Добавлено: TM35-MAIN-106»), запись
    # удалена после проверки. У /api/gantt/work нет парного «удалить»
    # эндпоинта — гонять реальное сохранение в каждом прогоне значило бы
    # копить тестовые работы в БД без возможности подчистки отсюда,
    # поэтому автоматический сценарий проверяет открытие/заполнение/отмену
    # (безопасно, без побочных эффектов), не сам факт записи в БД.
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    page.locator("[data-add-group]").first.click()
    page.wait_for_timeout(250)
    if not page.locator("#modal-new-work").evaluate('el => el.classList.contains("open")'):
        return False, "модалка не открылась"
    page.fill("#nw-name", "проверка полей (не будет сохранено)")
    page.fill("#nw-unit", "шт")
    page.fill("#nw-location", "УТ99")
    page.click("[data-close='modal-new-work']")
    page.wait_for_timeout(200)
    closed = not page.locator("#modal-new-work").evaluate('el => el.classList.contains("open")')
    if not closed:
        return False, "«Отмена» не закрыла модалку"
    return True, "открытие/заполнение/отмена работают; сохранение проверено вручную отдельно"


# ---------------------------------------------------------------------
# Календарь (dm-picker) — открытие/закрытие/навигация/клавиатура
# ---------------------------------------------------------------------

@scenario("Календарь: кнопка открывает попап")
def test_calendar_open(page, base_url):
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    picker = page.locator(".dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    ok = picker.locator(".dm-cal").is_visible()
    picker.locator(".dm-btn").click()
    return ok, "попап открылся" if ok else "попап не появился"


@scenario("Календарь: навигация «следующий/предыдущий месяц» НЕ закрывает попап")
def test_calendar_month_nav(page, base_url):
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    picker = page.locator(".dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    title0 = picker.locator(".dm-cal-title").inner_text()
    picker.locator(".dm-cal-nav").nth(1).click()  # →
    page.wait_for_timeout(150)
    open_after_next = picker.locator(".dm-cal").is_visible()
    title1 = picker.locator(".dm-cal-title").inner_text()
    picker.locator(".dm-cal-nav").nth(0).click()  # ‹
    page.wait_for_timeout(150)
    open_after_prev = picker.locator(".dm-cal").is_visible()
    title2 = picker.locator(".dm-cal-title").inner_text()
    picker.locator(".dm-btn").click()  # закрыть
    if not (open_after_next and open_after_prev):
        return False, f"закрылся после навигации: next_open={open_after_next} prev_open={open_after_prev}"
    if title1 == title0 or title2 != title0:
        return False, f"заголовки месяца не менялись корректно: {title0} -> {title1} -> {title2}"
    return True, f"{title0} -> {title1} -> {title2}, попап оставался открыт"


@scenario("Календарь: клик по дню выбирает дату и закрывает попап")
def test_calendar_pick_day(page, base_url):
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    picker = page.locator(".dm-picker").first
    hidden = picker.locator("input[type=hidden]")
    original = hidden.input_value()
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    day15 = picker.locator(".dm-cal-day").get_by_text("15", exact=True)
    day15.click()
    page.wait_for_timeout(200)
    closed = not picker.locator(".dm-cal").is_visible()
    new_val = hidden.input_value()
    # восстановление исходного значения без реального сохранения на сервер
    page.goto(base_url + "/status", wait_until="networkidle")
    if not closed or new_val == original or not new_val.endswith("-15"):
        return False, f"closed={closed} original={original} new={new_val}"
    return True, f"{original} -> {new_val}, попап закрылся"


@scenario("Календарь: Esc закрывает попап")
def test_calendar_esc(page, base_url):
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    picker = page.locator(".dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    picker.locator(".dm-text").press("Escape")
    page.wait_for_timeout(150)
    ok = not picker.locator(".dm-cal").is_visible()
    return ok, "закрылся по Esc" if ok else "остался открыт"


@scenario("Календарь: клик вне попапа закрывает его")
def test_calendar_click_outside(page, base_url):
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    picker = page.locator(".dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    page.locator("h1").click()
    page.wait_for_timeout(150)
    ok = not picker.locator(".dm-cal").is_visible()
    return ok, "закрылся по клику вне" if ok else "остался открыт"


@scenario("Календарь в модальном окне (/gantt): навигация тоже не закрывает попап")
def test_calendar_in_modal(page, base_url):
    page.goto(base_url + "/gantt", wait_until="networkidle")
    page.wait_for_timeout(700)
    page.locator("[data-open-work]").first.click()
    page.wait_for_timeout(300)
    picker = page.locator("#modal-work .dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    picker.locator(".dm-cal-nav").nth(1).click()
    page.wait_for_timeout(150)
    ok = picker.locator(".dm-cal").is_visible()
    page.click("[data-close='modal-work']")
    return ok, "остался открыт после навигации внутри модалки" if ok else "закрылся — регрессия бага"


# ---------------------------------------------------------------------
# Директивный срок — сохранение и пересчёт зависимых метрик
# ---------------------------------------------------------------------

@scenario("Директивный срок: сохранение пересчитывает зависимые метрики и переживает перезагрузку")
def test_directive_deadline(page, base_url):
    # Реальная, значимая для отчётности настройка (влияет на «Отклонение
    # прогноза от директивного срока» и на дефицит ресурса) — восстановление
    # исходного значения делается НАПРЯМУЮ через hidden-инпут + событие change
    # (не повторным кликом по календарю: подбор дня клик за кликом хрупок —
    # при сдвиге месяца легко промахнуться и молча оставить в БД не то
    # значение, с которым сценарий начал работу).
    page.goto(base_url + "/status", wait_until="networkidle")
    page.wait_for_timeout(500)
    original = page.locator("#dd-value").input_value()
    deficit_before = page.locator(".tile").nth(2).inner_text()

    picker = page.locator(".dm-picker").first
    picker.locator(".dm-btn").click()
    page.wait_for_timeout(150)
    picker.locator(".dm-cal-nav").nth(1).click()
    page.wait_for_timeout(150)
    picker.locator(".dm-cal-day").get_by_text("5", exact=True).click()
    page.wait_for_timeout(150)
    page.click("#dd-save")
    page.wait_for_timeout(1200)
    deficit_after = page.locator(".tile").nth(2).inner_text()
    changed_value = page.locator("#dd-value").input_value()

    # восстановление исходного значения — напрямую, без похода через календарь
    page.evaluate(
        """
        (iso) => {
          var el = document.getElementById('dd-value');
          el.value = iso;
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """,
        original,
    )
    page.wait_for_timeout(150)
    page.click("#dd-save")
    page.wait_for_timeout(1200)
    restored = page.locator("#dd-value").input_value()

    if changed_value == original:
        return False, "значение не изменилось после сохранения"
    if deficit_before == deficit_after:
        return False, f"зависимая метрика не пересчиталась: {deficit_before!r} == {deficit_after!r}"
    if restored != original:
        return False, f"НЕ УДАЛОСЬ восстановить исходное значение: было {original}, осталось {restored} — ПРОВЕРИТЬ И ИСПРАВИТЬ В БД ВРУЧНУЮ (app_setting.directive_deadline)"
    return True, f"{deficit_before} -> {deficit_after}, восстановлено {restored}"


# ---------------------------------------------------------------------
# Рапорт — печать/экспорт
# ---------------------------------------------------------------------

@scenario("Рапорт: печатная версия скрывает меню и панель, оставляет документ")
def test_report_print(page, base_url):
    page.goto(base_url + "/report", wait_until="networkidle")
    page.wait_for_timeout(400)
    page.emulate_media(media="print")
    page.wait_for_timeout(200)
    nav_hidden = not page.locator("nav").is_visible()
    toolbar_hidden = not page.locator(".report-toolbar").is_visible()
    doc_visible = page.locator(".report-doc").is_visible()
    page.emulate_media(media="screen")
    if not (nav_hidden and toolbar_hidden and doc_visible):
        return False, f"nav_hidden={nav_hidden} toolbar_hidden={toolbar_hidden} doc_visible={doc_visible}"
    return True, "печатная версия корректна"


# ---------------------------------------------------------------------
# Ссылки между экранами
# ---------------------------------------------------------------------

REGISTERED_GET_ROUTES = {
    "/", "/status", "/today", "/report", "/losses", "/data", "/dashboard",
    "/critical", "/works", "/norms", "/norm-plan", "/resources", "/downtime",
    "/subcontractors", "/materials", "/blockers", "/daily-report", "/executor",
    "/quality", "/form", "/gantt", "/healthz",
}


@scenario("Ссылки между экранами ведут на существующие маршруты")
def test_internal_links(page, base_url):
    bad = []
    for path in sorted(REGISTERED_GET_ROUTES - {"/healthz"}):
        page.goto(base_url + path, wait_until="networkidle")
        page.wait_for_timeout(200)
        hrefs = page.eval_on_selector_all(
            "a[href^='/']", "els => els.map(e => e.getAttribute('href').split('?')[0])"
        )
        for href in hrefs:
            if href not in REGISTERED_GET_ROUTES and not href.startswith("/static"):
                bad.append(f"{path} -> {href}")
    if bad:
        return False, "; ".join(bad)
    return True, f"проверено {len(REGISTERED_GET_ROUTES) - 1} страниц, все ссылки валидны"


ALL_SCENARIOS = [
    test_simple_other_opens_comment,
    test_simple_other_requires_comment,
    test_simple_other_persists,
    test_simple_invalid_crew,
    test_gantt_date_nav,
    test_gantt_location_filter,
    test_gantt_active_only,
    test_gantt_apply_combo,
    test_gantt_group_toggle,
    test_gantt_cell_edit,
    test_gantt_sticky_column,
    test_gantt_add_work_modal,
    test_calendar_open,
    test_calendar_month_nav,
    test_calendar_pick_day,
    test_calendar_esc,
    test_calendar_click_outside,
    test_calendar_in_modal,
    test_directive_deadline,
    test_report_print,
    test_internal_links,
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://tm.asd-kontur.ru")
    args = ap.parse_args()

    user = os.environ.get("TM_BASIC_AUTH_USER")
    pw = os.environ.get("TM_BASIC_AUTH_PASSWORD")
    if not user or not pw:
        env_path = Path(__file__).resolve().parent.parent / ".secrets" / "tm_basic_auth.env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            user = os.environ.get("TM_BASIC_AUTH_USER")
            pw = os.environ.get("TM_BASIC_AUTH_PASSWORD")
    if not user or not pw:
        print("Нет учётных данных Basic Auth", file=sys.stderr)
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(http_credentials={"username": user, "password": pw})
        page = context.new_page()
        for fn in ALL_SCENARIOS:
            fn(page, args.base_url)
        browser.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\nВсего сценариев: {len(RESULTS)}. Провалено: {len(failed)}.")
    if failed:
        print("\n=== ПРОВАЛЕННЫЕ СЦЕНАРИИ ===", file=sys.stderr)
        for name, ok, detail in failed:
            print(f"  {name}: {detail}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
