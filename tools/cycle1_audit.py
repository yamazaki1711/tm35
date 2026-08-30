#!/usr/bin/env python3
"""
Цикл 1 сплошной проверки (29.08.2026) — обновление tools/ui_audit.py:
- basic-auth снят со всего сайта 29.08 -> вход теперь через /login формой,
  не http_credentials;
- список страниц расширен с 21 до полного набора человеческих GET-
  маршрутов (по grep main.py в контейнере: 32 включая redirect "/");
- добавлен перехват консольных ошибок и сетевых 4xx/5xx (ui_audit.py
  этого не делал вообще);
- две резолюции по заданию цикла 1: 1366x768, 1920x1080 (не три, как
  в общем правиле CLAUDE.md — используется явное задание координатора
  для этого прохода).

Использование:
    python3 tools/cycle1_audit.py --role admin --out DIR
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forbidden_strings import scan as scan_forbidden  # noqa: E402

BASE_URL = "https://tm.asd-kontur.ru"

# (path, title, in_primary_nav, in_data_hub)
PAGES = [
    ("/dashboard", "Обзор", True, True),
    ("/shift", "Экран смены", True, False),
    ("/form", "Ввод факта (форма)", True, True),
    ("/baseline", "Плановые сроки", True, True),
    ("/blockers", "Ограничения", True, True),
    ("/status", "Успеваем?", True, False),
    ("/calculator", "Калькулятор", True, False),
    ("/today", "Сегодня", True, False),
    ("/report", "Рапорт", True, False),
    ("/gantt", "График", True, True),
    ("/id-entry", "Ввод ИД", True, False),
    ("/id-packages", "Пакеты ИД", True, False),
    ("/changes", "ИЗМ", True, False),
    ("/prescriptions", "Предписания", True, False),
    ("/data", "Данные (хаб)", True, False),
    ("/critical", "Критичные работы", False, True),
    ("/works", "Реестр работ", False, True),
    ("/resources", "Ресурсы", False, True),
    ("/downtime", "Простои", False, True),
    ("/subcontractors", "Субподрядчики", False, True),
    ("/materials", "Материалы и поставки", False, True),
    ("/daily-report", "Ежедневная сводка", False, True),
    ("/executor", "Обоснование Исполнителя", False, True),
    ("/quality", "Качество данных", False, True),
    ("/norms", "Справочник норм", False, True),
    ("/norm-plan", "Плановый график (56 позиций)", False, True),
    ("/settings", "Настройки объекта", False, True),
    ("/settings/users", "Учётные записи (admin)", False, False),
    ("/journal", "Журнал входов (admin)", False, False),
    ("/losses", "Почему отстаём (известный 500)", False, False),
    ("/", "Корень (редирект на /dashboard)", False, False),
    ("/login", "Вход", False, False),
]

VIEWPORTS = [(1366, 768), (1920, 1080)]

CREDENTIALS = {
    "admin": ("admin", "g5773c89"),
    "denisov": ("denisov", "8fnnytiw"),
}


def collect_text_and_forbidden(page):
    body_text = page.evaluate(
        """
        () => {
          const clone = document.body.cloneNode(true);
          clone.querySelectorAll('script, style').forEach(el => el.remove());
          return clone.textContent || '';
        }
        """
    )
    title_attrs = page.evaluate(
        "Array.from(document.querySelectorAll('[title]')).map(el => el.getAttribute('title')).join(' \\n ')"
    )
    aria_attrs = page.evaluate(
        "Array.from(document.querySelectorAll('[aria-label]')).map(el => el.getAttribute('aria-label')).join(' \\n ')"
    )
    svg_text = page.evaluate(
        "Array.from(document.querySelectorAll('svg text, svg title, svg desc')).map(el => el.textContent).join(' \\n ')"
    )
    combined = "\n".join([body_text, title_attrs, aria_attrs, svg_text])
    hits = scan_forbidden(combined)
    return [{"type": "forbidden_text", "label": label, "match": match, "context": ctx} for label, match, ctx in hits]


def layout_checks(page):
    issues = []
    layout = page.evaluate(
        """
        () => ({
          docScrollWidth: document.documentElement.scrollWidth,
          docClientWidth: document.documentElement.clientWidth,
        })
        """
    )
    if layout["docScrollWidth"] > layout["docClientWidth"] + 2:
        issues.append({
            "type": "horizontal_scroll", "label": "горизонтальная прокрутка страницы",
            "match": f"scrollWidth={layout['docScrollWidth']} clientWidth={layout['docClientWidth']}", "context": "",
        })
    return issues


def login(page, role):
    user, pw = CREDENTIALS[role]
    page.goto(BASE_URL + "/login", wait_until="networkidle", timeout=30000)
    page.fill('input[name="login"]', user)
    page.fill('input[name="password"]', pw)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=30000)
    if "/login" in page.url:
        raise RuntimeError(f"login failed for role={role}, still on {page.url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="admin", choices=list(CREDENTIALS.keys()) + ["anon"])
    ap.add_argument("--out", default="/tmp/claude-1000/-home-oleg/71549f48-2782-42b4-bcdc-b71de7e2629e/scratchpad/tm35_audit/cycle1")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    out_dir = Path(args.out) / args.role
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = PAGES
    if args.only:
        wanted = set(args.only.split(","))
        pages = [p for p in PAGES if p[0] in wanted]

    report = {}
    forbidden_total = 0
    console_total = 0
    network_total = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context()
        page = context.new_page()

        if args.role != "anon":
            login(page, args.role)

        console_errors = []
        network_errors = []

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        def on_response(resp):
            if resp.status >= 400:
                network_errors.append(f"{resp.status} {resp.url}")

        page.on("console", on_console)
        page.on("response", on_response)

        for path, title, in_nav, in_hub in pages:
            report[path] = {"title": title, "in_primary_nav": in_nav, "in_data_hub": in_hub, "by_viewport": {}}
            for w, h in VIEWPORTS:
                console_errors.clear()
                network_errors.clear()
                page.set_viewport_size({"width": w, "height": h})
                try:
                    resp = page.goto(BASE_URL + path, wait_until="networkidle", timeout=30000)
                    status = resp.status if resp else None
                except Exception as e:
                    status = f"EXC: {e}"
                page.wait_for_timeout(400)

                issues = []
                try:
                    issues += collect_text_and_forbidden(page)
                except Exception as e:
                    issues.append({"type": "scan_error", "label": str(e), "match": "", "context": ""})
                issues += layout_checks(page)

                vp_key = f"{w}x{h}"
                fname = f"{path.strip('/').replace('/', '_') or 'root'}__{vp_key}.png"
                page.screenshot(path=str(out_dir / fname), full_page=True)

                report[path]["by_viewport"][vp_key] = {
                    "http_status": status,
                    "issues": issues,
                    "console_errors": list(console_errors),
                    "network_errors": list(network_errors),
                    "screenshot": fname,
                }
                forbidden_total += sum(1 for i in issues if i["type"] == "forbidden_text")
                console_total += len(console_errors)
                network_total += len(network_errors)
                print(f"[{args.role}] {path:20s} {vp_key:10s} status={status} "
                      f"issues={len(issues)} console_err={len(console_errors)} net_err={len(network_errors)}")

        browser.close()

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== role={args.role} ===")
    print(f"Запрещённых строк: {forbidden_total}. Консольных ошибок: {console_total}. Сетевых 4xx/5xx: {network_total}.")
    print(f"Отчёт: {out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
