#!/usr/bin/env python3
"""
Автоматическая проверка интерфейса ТМ-35 — сплошной проход по всем
экранам, повторяемая (прогонять после КАЖДОЙ правки интерфейса, до
отчёта о выполнении — координатор, 20.08.2026).

ТЗ координатора 16.09.2026 ("текст, который не помещается, переносится,
а не обрезается — сделать это проверяемым"): до этой правки скрипт был
написан и НИ РАЗУ не задеплоен (`docs/PROD_SYNC_2026-09-15.md`, §3) —
список страниц был жёстко зашит (`PAGES`), проверки шрифта/прокрутки
только печатались, не проваливали прогон. Теперь:

1. Список маршрутов берётся из СОБСТВЕННОЙ таблицы роутинга приложения
   (`app.routes`) — новая страница появляется в проверке в день своего
   появления, без правки этого файла. Берутся только GET-маршруты без
   параметров пути (`{id}` и т.п. — их не с чем подставить вслепую) и не
   являющиеся `/api/`, `/static/`, `/export/`. Маршрут, которому для
   осмысленного ответа всё равно нужен обязательный query-параметр,
   вернёт не-200 — это печатается как "пропущен", не проваливает прогон
   (проверять его — отдельная страница с реальным id, вне этого прохода).

2. Запрещённые строки (tools/forbidden_strings.py — единый список),
   как и раньше — сканируется НЕ только видимый текст (innerText), но и
   содержимое свёрнутых блоков/модалок, текст внутри SVG, атрибуты
   title/aria-label. Ненулевой код при любом нарушении.

3. Новое — четыре измеримых дефекта из ТЗ 16.09.2026, ТЕПЕРЬ ПРОВАЛИВАЮТ
   прогон (ненулевой код), не просто печатаются:
   - горизонтальная прокрутка ВСЕЙ страницы (document.scrollWidth >
     clientWidth) — собственная прокрутка `.table-wrap`/`.id-matrix-wrap`
     внутри контейнера сюда не попадает, это разные вещи: контейнер
     держит своё содержимое в себе, страница вокруг не двигается;
   - любой видимый текст мельче 14px;
   - любой элемент, чья правая граница выходит за правый край viewport —
     кроме элементов ВНУТРИ контейнера с собственным overflow-x
     (auto/scroll) — тот контейнер (сетка ИД, /gantt, /shift) обязан
     прокручиваться сам, это его законная работа, не дефект;
   - любая ячейка таблицы, где scrollWidth > clientWidth (обрезанный,
     невидимый остаток текста) — тот же вынос за контейнер со
     собственной прокруткой не считается (там clip — Gantt, `.col-name`,
     единственное задокументированное исключение с явным многоточием,
     не молчаливая обрезка).

   Высота строки/ширина контейнера ("main ~90% окна") остаются
   информационными находками — не о переносе текста, не в периметре ТЗ
   16.09.2026, ненулевой код не дают.

Использование (внутри контейнера tm_backend):
    python3 tools/ui_audit.py [--base-url URL] [--out DIR] [--no-shots] [--only /a,/b]

Базовый URL по умолчанию — сам контейнер (http://localhost:8000), без
внешнего DNS/TLS и без Basic Auth: тот был снят со всего сайта
29.08.2026 (docs/AUTH_2026-08-29.md) — TM_BASIC_AUTH_USER/PASSWORD
поддержаны для обратной совместимости (если когда-нибудь понадобятся
снова), но не обязательны.

Требует playwright с установленным Chrome — на 16.09.2026 подтверждено
установленным в контейнере (`p.chromium.launch(channel="chrome")`
и bundled chromium оба живые), устанавливать заново не пришлось.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/app")
from forbidden_strings import scan as scan_forbidden  # noqa: E402

VIEWPORTS = [(1920, 1080), (1366, 768), (2560, 1440)]

# Маршруты, которые технически GET без параметров пути, но не страницы
# для человека (экспорт файлов, служебные "прочитать и уйти") — шумели
# бы в прогоне без пользы. Явный, короткий список причин, не эвристика.
SKIP_PREFIXES = ("/api/", "/static/", "/export/")
# /docs, /redoc, /openapi.json — служебные маршруты FastAPI (Swagger/
# ReDoc), не экраны приложения; /docs тянет CDN-скрипт и без внешнего
# интернета из контейнера виснет на networkidle (найдено 16.09.2026).
SKIP_EXACT = {"/healthz", "/health", "/docs", "/redoc", "/openapi.json"}


def discover_routes():
    """Список маршрутов — из app.routes (FastAPI), не из ручного списка.
    Только GET, только без {param} в пути (их не с чем подставить вслепую
    в общем прогоне — раздельные экраны с реальным id проверяются точечно,
    не этим проходом)."""
    import main as m

    routes = []
    seen = set()
    for r in m.app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods or "GET" not in methods:
            continue
        if "{" in path:
            continue
        if path in SKIP_EXACT or any(path.startswith(p) for p in SKIP_PREFIXES):
            continue
        if path in seen:
            continue
        seen.add(path)
        routes.append(path)
    routes.sort()
    return routes


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

# Общий JS-хелпер: элемент лежит внутри контейнера с собственной
# горизонтальной прокруткой (overflow-x: auto/scroll)? Такие контейнеры
# (сетка ИД `.id-matrix-wrap`, `/gantt` `#gantt-scroll`, `/shift`
# `.table-wrap` с table-layout:fixed) обязаны прокручиваться сами —
# единственное разрешённое место, где что-то "выходит за край" законно.
IN_SCROLL_CONTAINER_JS = """
  function inScrollContainer(el) {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const cs = getComputedStyle(node);
      if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') && node.scrollWidth > node.clientWidth + 1) {
        return true;
      }
      node = node.parentElement;
    }
    return false;
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

    # --- ТЗ 16.09.2026, дефект 1: любой видимый текст мельче 14px ---
    small_font = page.evaluate(
        """
        () => {
          const bad = [];
          document.querySelectorAll('body *').forEach(el => {
            if (el.children.length > 0) return;
            const txt = (el.textContent || '').trim();
            if (!txt) return;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none') return;
            const size = parseFloat(cs.fontSize);
            if (size < 14 && bad.length < 30) {
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
            "fatal": True,
        })

    # --- ТЗ 16.09.2026, дефект 2: горизонтальная прокрутка ВСЕЙ страницы ---
    layout = page.evaluate(
        """
        () => {
          const main = document.querySelector('main');
          const r = main ? main.getBoundingClientRect() : null;
          return {
            winWidth: window.innerWidth,
            mainWidth: r ? r.width : null,
            leftMargin: r ? r.left : null,
            rightMargin: r ? (window.innerWidth - r.right) : null,
            docScrollWidth: document.documentElement.scrollWidth,
            docClientWidth: document.documentElement.clientWidth,
          };
        }
        """
    )
    if layout["docScrollWidth"] > layout["docClientWidth"] + 2:
        issues.append({
            "type": "horizontal_scroll", "label": "горизонтальная прокрутка страницы", "fatal": True,
            "match": f"scrollWidth={layout['docScrollWidth']} clientWidth={layout['docClientWidth']}", "context": "",
        })
    if layout["mainWidth"] is not None:
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

    # --- ТЗ 16.09.2026, дефект 3: элемент выходит за правый край viewport ---
    off_viewport = page.evaluate(
        """
        () => {
""" + IN_SCROLL_CONTAINER_JS + """
          const bad = [];
          const vw = document.documentElement.clientWidth;
          document.querySelectorAll('body *').forEach(el => {
            if (bad.length >= 20) return;
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            if (r.right > vw + 2 && !inScrollContainer(el)) {
              bad.push({tag: el.tagName, cls: String(el.className), right: Math.round(r.right),
                        text: (el.textContent || '').trim().slice(0, 40)});
            }
          });
          return bad;
        }
        """
    )
    for o in off_viewport:
        issues.append({
            "type": "off_viewport", "label": f"правый край за viewport ({o['right']}px)", "fatal": True,
            "match": o["text"], "context": f"<{o['tag']} class=\"{o['cls']}\">",
        })

    # --- ТЗ 16.09.2026, дефект 4: обрезанная (невидимая) ячейка таблицы ---
    clipped_cells = page.evaluate(
        """
        () => {
""" + IN_SCROLL_CONTAINER_JS + """
          const bad = [];
          document.querySelectorAll('td, th').forEach(el => {
            if (bad.length >= 20) return;
            const cs = getComputedStyle(el);
            if (cs.textOverflow === 'ellipsis') return;  // явный, видимый клип — не молчаливая обрезка
            if (el.scrollWidth > el.clientWidth + 1 && !inScrollContainer(el)) {
              bad.push({tag: el.tagName, cls: String(el.className),
                        text: (el.textContent || '').trim().slice(0, 40),
                        scrollWidth: el.scrollWidth, clientWidth: el.clientWidth});
            }
          });
          return bad;
        }
        """
    )
    for c in clipped_cells:
        issues.append({
            "type": "clipped_cell", "label": f"обрезана: scrollWidth={c['scrollWidth']} > clientWidth={c['clientWidth']}",
            "match": c["text"], "context": f"<{c['tag']} class=\"{c['cls']}\">", "fatal": True,
        })

    # --- Координатор, 19.09.2026: строка плиток .kpi-row перенеслась
    # ("хвост") — auto-fit считал, сколько колонок ПОМЕЩАЕТСЯ по
    # 200px, не сколько плиток реально в строке (см. style.css). Ряд
    # плиток обязан быть одной строкой — offsetTop у всех .kpi внутри
    # одного .kpi-row должен совпадать; больше одного различного
    # значения = перенос, с "хвостом" или без.
    kpi_wraps = page.evaluate(
        """
        () => {
          const bad = [];
          document.querySelectorAll('.kpi-row').forEach((row, ri) => {
            const tops = Array.from(row.querySelectorAll(':scope > .kpi'))
              .map(el => el.offsetTop);
            const distinct = Array.from(new Set(tops));
            if (distinct.length > 1) {
              bad.push({row: ri, count: tops.length, distinctTops: distinct.length,
                        text: (row.innerText || '').trim().slice(0, 60)});
            }
          });
          return bad;
        }
        """
    )
    for k in kpi_wraps:
        issues.append({
            "type": "kpi_row_wrap",
            "label": f"строка плиток перенеслась: {k['distinctTops']} разных offsetTop у {k['count']} плиток",
            "match": k["text"], "context": f".kpi-row #{k['row']}", "fatal": True,
        })

    # --- Информационная находка (не дефект переноса, не проваливает прогон) ---
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

    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--out", default=f"/tmp/ui_audit_{int(time.time())}")
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
    # Basic auth снят со всего сайта 29.08.2026 (docs/AUTH_2026-08-29.md) —
    # креды поддержаны для обратной совместимости, но не обязательны.

    routes = discover_routes()
    if args.only:
        wanted = set(args.only.split(","))
        routes = [p for p in routes if p in wanted]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {}
    skipped = []
    forbidden_total = 0
    fatal_total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context_kwargs = {}
        if user and pw:
            context_kwargs["http_credentials"] = {"username": user, "password": pw}
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        for path in routes:
            report[path] = {"by_viewport": {}}
            for w, h in VIEWPORTS:
                page.set_viewport_size({"width": w, "height": h})
                resp = page.goto(args.base_url + path, wait_until="networkidle", timeout=30000)
                if resp is not None and resp.status != 200:
                    skipped.append((path, w, h, resp.status))
                    print(f"{path:30s} {w}x{h:<6d} — пропущен, HTTP {resp.status} (нужен параметр запроса?)")
                    continue
                page.wait_for_timeout(400)
                try:
                    page.evaluate(EXPAND_JS)
                except Exception:
                    pass
                page.wait_for_timeout(150)
                issues = audit_page(page)
                report[path]["by_viewport"][f"{w}x{h}"] = issues
                n_forbidden = sum(1 for i in issues if i["type"] == "forbidden_text")
                n_fatal = sum(1 for i in issues if i.get("fatal"))
                forbidden_total += n_forbidden
                fatal_total += n_fatal
                if not args.no_shots:
                    fname = f"{path.strip('/').replace('/', '_') or 'home'}__{w}x{h}.png"
                    page.screenshot(path=str(out_dir / fname), full_page=True)
                print(f"{path:30s} {w}x{h:<6d} — {len(issues)} находок "
                      f"(запрещённых строк: {n_forbidden}, провальных: {n_fatal})")

        browser.close()

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for r in report.values() for v in r["by_viewport"].values())
    print(f"\nМаршрутов проверено: {len(routes)}, пропущено (не 200): {len(skipped)}.")
    print(f"Всего находок: {total}. Провальных: {fatal_total}. Запрещённых строк: {forbidden_total}.")
    print(f"Отчёт: {out_dir / 'report.json'}")

    failed = forbidden_total > 0 or fatal_total > 0
    if failed:
        print("\n=== ПРОВЕРКА НЕ ПРОЙДЕНА ===", file=sys.stderr)
        for path, data in report.items():
            for vp, issues in data["by_viewport"].items():
                for it in issues:
                    if it["type"] == "forbidden_text" or it.get("fatal"):
                        print(f"  {path} [{vp}] {it['label']}: {it['match']!r} — …{it['context']}…", file=sys.stderr)
        sys.exit(1)

    print("\nПровальных находок и запрещённых строк не найдено ни на одной странице.")
    sys.exit(0)


if __name__ == "__main__":
    main()
