#!/usr/bin/env python3
"""
Автоматическая проверка интерфейса ТМ-35 — сплошной проход по всем
экранам, повторяемая (прогонять после КАЖДОЙ правки интерфейса, до
отчёта о выполнении — координатор, 20.08.2026).

Две категории проверок:
1. Запрещённые строки (tools/forbidden_strings.py — единый список).
   Сканируется НЕ только видимый текст (innerText), но и:
   - содержимое свёрнутых блоков/модальных окон (через textContent,
     который в отличие от innerText не зависит от display:none);
   - текст внутри SVG (<text>, <title>, <desc>) — легенды и подписи
     диаграмм строятся в JS, не в шаблонах, и обычный просмотр HTML их
     не видит;
   - атрибуты title/aria-label (всплывающие подсказки).
   Скрипт завершается с ненулевым кодом, если найдено хоть одно
   нарушение — этого достаточно, чтобы не отчитываться "исправлено" по
   ошибке.
2. Остальное — размеры шрифта, высота строк таблиц, ширина контейнера,
   горизонтальная прокрутка. Не приводит к ненулевому коду (это
   вспомогательные находки, часть визуальной приёмки), но печатается.

Использование:
    python3 tools/ui_audit.py [--base-url URL] [--out DIR] [--no-shots] [--only /a,/b]

Требует переменные окружения TM_BASIC_AUTH_USER/TM_BASIC_AUTH_PASSWORD
(см. .secrets/tm_basic_auth.env) и playwright с установленным Chrome.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forbidden_strings import scan as scan_forbidden  # noqa: E402

PAGES = [
    ("/", "Простой ввод факта"),
    ("/status", "Успеваем?"),
    ("/today", "Сегодня"),
    ("/report", "Рапорт"),
    ("/losses", "Почему отстаём"),
    ("/data", "Данные (хаб)"),
    ("/dashboard", "Данные → Главная (метрики)"),
    ("/critical", "Данные → Критичные работы"),
    ("/works", "Данные → Реестр работ"),
    ("/resources", "Данные → Ресурсы"),
    ("/downtime", "Данные → Простои"),
    ("/subcontractors", "Данные → Субподрядчики"),
    ("/materials", "Данные → Материалы и поставки"),
    ("/blockers", "Данные → Ограничения"),
    ("/daily-report", "Данные → Ежедневная сводка"),
    ("/executor", "Данные → Обоснование Исполнителя"),
    ("/quality", "Данные → Качество данных"),
    ("/form", "Данные → Ввод факта (форма)"),
    ("/gantt", "Данные → Интерактивный график"),
    ("/norms", "Данные → Справочник норм"),
    ("/norm-plan", "Данные → Плановый график"),
]

VIEWPORTS = [(1920, 1080), (1366, 768), (2560, 1440)]

# Раскрывающиеся элементы, которые нужно принудительно открыть перед
# сканированием (иначе их содержимое не появится в DOM/останется
# display:none, а собственный textContent-скан покрывает только то, что
# УЖЕ есть в дереве, а не то, что подгружается по клику).
EXPAND_JS = """
() => {
  document.querySelectorAll('.group-row.collapsed').forEach(el => el.classList.remove('collapsed'));
  document.querySelectorAll('[style*="display: none"], [style*="display:none"]').forEach(el => {
    if (el.classList.contains('modal-overlay')) return;  // модалки открываем отдельно кликом
    el.style.display = '';
  });
  document.querySelectorAll('.modal-overlay').forEach(el => el.classList.add('open'));
}
"""


def collect_text_and_forbidden(page):
    # textContent по клону body с вырезанными <script>/<style> — обычный
    # innerText игнорирует display:none (значит не видит свёрнутые блоки
    # и данные, которые JS ещё не вставил), а textContent на живом body
    # без вырезания читает исходный код JS/JSON-пейлоады внутри <script>
    # как если бы это был текст на странице (ложные срабатывания —
    # проверено на находке: даты внутри window.TM35_STATUS = {...}).
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


def audit_page(page):
    issues = collect_text_and_forbidden(page)

    small_font = page.evaluate(
        """
        () => {
          const bad = [];
          document.querySelectorAll('body *').forEach(el => {
            if (el.children.length > 0) return;
            const txt = (el.textContent || '').trim();
            if (!txt) return;
            const cs = getComputedStyle(el);
            const size = parseFloat(cs.fontSize);
            if (size < 14 && bad.length < 20) {
              bad.push({tag: el.tagName, cls: String(el.className), text: txt.slice(0, 40), size});
            }
          });
          return bad;
        }
        """
    )
    for b in small_font:
        issues.append({
            "type": "small_font", "label": f"шрифт {b['size']}px < 14px",
            "match": b["text"], "context": f"<{b['tag']} class=\"{b['cls']}\">",
        })

    short_rows = page.evaluate(
        """
        () => {
          const bad = [];
          document.querySelectorAll('table').forEach((t, ti) => {
            t.querySelectorAll('tr').forEach((tr, ri) => {
              if (tr.offsetHeight > 0 && tr.offsetHeight < 40 && bad.length < 10) {
                bad.push({table: ti, row: ri, height: tr.offsetHeight, text: (tr.innerText||'').slice(0,40)});
              }
            });
          });
          return bad;
        }
        """
    )
    for r in short_rows:
        issues.append({
            "type": "short_row", "label": f"высота строки {r['height']}px < 40px",
            "match": r["text"], "context": f"таблица #{r['table']}, строка #{r['row']}",
        })

    layout = page.evaluate(
        """
        () => {
          const main = document.querySelector('main');
          if (!main) return null;
          const r = main.getBoundingClientRect();
          return {
            winWidth: window.innerWidth, mainWidth: r.width,
            leftMargin: r.left, rightMargin: window.innerWidth - r.right,
            docScrollWidth: document.documentElement.scrollWidth,
            docClientWidth: document.documentElement.clientWidth,
          };
        }
        """
    )
    if layout:
        pct = layout["mainWidth"] / layout["winWidth"] * 100
        if not (85 <= pct <= 95):
            issues.append({
                "type": "container_width", "label": f"main={pct:.1f}% окна (ожидалось ~90%)",
                "match": f"{layout['mainWidth']:.0f}px из {layout['winWidth']}px",
                "context": f"left={layout['leftMargin']:.0f}px right={layout['rightMargin']:.0f}px",
            })
        elif abs(layout["leftMargin"] - layout["rightMargin"]) > 4:
            issues.append({
                "type": "container_asymmetric", "label": "отступы слева/справа не равны",
                "match": f"left={layout['leftMargin']:.0f}px right={layout['rightMargin']:.0f}px", "context": "",
            })
        if layout["docScrollWidth"] > layout["docClientWidth"] + 2:
            issues.append({
                "type": "horizontal_scroll", "label": "горизонтальная прокрутка страницы",
                "match": f"scrollWidth={layout['docScrollWidth']} clientWidth={layout['docClientWidth']}", "context": "",
            })

    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://tm.asd-kontur.ru")
    ap.add_argument("--out", default="/tmp/claude-1000/-home-oleg/227be90a-e982-4f69-bfe3-129615d5f18e/scratchpad/ui_audit/after")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--only", default=None)
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
        print("Нет учётных данных Basic Auth (TM_BASIC_AUTH_USER/PASSWORD)", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = PAGES
    if args.only:
        wanted = set(args.only.split(","))
        pages = [p for p in PAGES if p[0] in wanted]

    report = {}
    forbidden_total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(http_credentials={"username": user, "password": pw})
        page = context.new_page()

        for path, title in pages:
            report[path] = {"title": title, "by_viewport": {}}
            for w, h in VIEWPORTS:
                page.set_viewport_size({"width": w, "height": h})
                page.goto(args.base_url + path, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(400)
                try:
                    page.evaluate(EXPAND_JS)
                except Exception:
                    pass
                page.wait_for_timeout(150)
                issues = audit_page(page)
                report[path]["by_viewport"][f"{w}x{h}"] = issues
                forbidden_total += sum(1 for i in issues if i["type"] == "forbidden_text")
                if not args.no_shots:
                    fname = f"{path.strip('/').replace('/', '_') or 'home'}__{w}x{h}.png"
                    page.screenshot(path=str(out_dir / fname), full_page=True)
                n_forbidden = sum(1 for i in issues if i["type"] == "forbidden_text")
                print(f"{path:20s} {w}x{h:<6d} — {len(issues)} находок (запрещённых строк: {n_forbidden})")

        browser.close()

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for r in report.values() for v in r["by_viewport"].values())
    print(f"\nВсего находок: {total}. Из них запрещённых строк: {forbidden_total}. Отчёт: {out_dir / 'report.json'}")

    if forbidden_total > 0:
        print("\n=== ЗАПРЕЩЁННЫЕ СТРОКИ — ПРОВЕРКА НЕ ПРОЙДЕНА ===", file=sys.stderr)
        for path, data in report.items():
            for vp, issues in data["by_viewport"].items():
                for it in issues:
                    if it["type"] == "forbidden_text":
                        print(f"  {path} [{vp}] {it['label']}: {it['match']!r} — …{it['context']}…", file=sys.stderr)
        sys.exit(1)

    print("\nЗапрещённых строк не найдено ни на одной странице.")
    sys.exit(0)


if __name__ == "__main__":
    main()
