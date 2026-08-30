"""
Прямая оцифровка норм ЕНиР (переделка по указанию координатора —
предыдущая версия строила join со сметой ТМ-35 и не извлекала сами
числа Н.вр., что оказалось бесполезно).

Метод: разбираем HTML (уже сконвертированный libreoffice из .doc/.rtf,
см. extract_enir.py::html_to_text — тот подход остаётся для заголовков
§/состава звена) НЕ построчным текстом, а через BeautifulSoup, сохраняя
реальную структуру таблиц (<table><tr><td>, colspan). Голый текстовый
парсинг ломает 2D-сетку таблицы и не даёт надёжно понять, какое число
Н.вр. относится к какому диаметру/массе/способу — что и было основной
причиной, почему в первой версии числа не извлекались вообще.

Наблюдение, проверенное на 4 разных сборниках (Е22, Е5, Е26, Е9) —
ячейка вида "<число> <число>-<число>" почти всегда означает
"Н.вр. Расц." в одной ячейке (напр. "0,65 0-48,4" -> Н.вр.=0,65). Это
самый надёжный сигнал, есть не в каждой таблице — там, где его нет,
ищем отдельную строку/столбец с явной подписью "Н.вр.". Если ни то, ни
другое не находится — параграф помечается как "не удалось разобрать
таблицу автоматически", числа НЕ придумываются (в архиве попадаются
таблицы, где ячейки Н.вр. пустые уже в самом .doc — не только
Е9-2-2/3/4, проверено прямо в HTML-исходнике, это дефект архива, не
парсера).
"""
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

CODE_RE = re.compile(r"^\s*(Е\s?\d+[а-я]?-\d+(?:-\d+)?[а-я]?)\.\s*(.*)$", re.UNICODE)
PAIR_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s+(\d+-\d+(?:[.,]\d+)?)$")
# "Н.вр. (маш.-ч) Расц." в одной ячейке — конвенция сборников с
# механизацией (Е2/Е12/Е17/Е20 — экскаваторы/бульдозеры/грейдеры):
# первое число — чел-час, в скобках — машино-час (не нужен для расчёта
# трудозатрат людей, отбрасываем), последнее — расценка.
PAIR_MACH_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*\(\d+(?:[.,]\d+)?\)\s*\d+-\d+(?:[.,]\d+)?(?:\s*\([^)]*\))?$")
BARE_NUM_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
LETTER_LEGEND = set("абвгдежзиклмнопрstu") | {"№", "N"}

STOP_HEADERS = {"Состав", "Указания", "Нормы", "Технические", "Организация",
                 "Примечание", "Примечания", "Таблица"}
UNIT_STOP = r"(?=\.|\s(?:Способ|Состав|Указания|Технические|Организация|Таблица|Примечание)\b|$)"
UNIT_RE = re.compile(r"[Нн]орм[ыа]\s+времени[^.]*?\bна\s+(.+?)" + UNIT_STOP, re.UNICODE)


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


HEADER_START_RE = re.compile(r"^§\s*(Е\s?\d+[а-я]?-\d+(?:-\d+)?[а-я]?)\.\s*(.*)$", re.UNICODE)


def find_headers(children):
    """Индексы детей <body>, где начинается новый § — маркер '§' и код
    лежат в ОДНОМ параграфе (проверено на реальном HTML), не в отдельных
    соседних, как можно было бы предположить по плоскому текстовому
    экспорту."""
    idxs = []
    for i, c in enumerate(children):
        if getattr(c, "name", None) is None:
            continue
        txt = clean(c.get_text()) if hasattr(c, "get_text") else ""
        if HEADER_START_RE.match(txt):
            idxs.append(i)
    return idxs


def parse_header_and_crew(children, header_idx, end):
    """header_idx указывает на параграф вида '§ Е22-1-9а. Название...'
    целиком; end — начало следующего §. Извлекает код, название, единицу,
    состав звена."""
    header_text = clean(children[header_idx].get_text())
    m = HEADER_START_RE.match(header_text)
    if not m:
        return None
    code, title = m.group(1).replace(" ", ""), clean(m.group(2))

    texts = []
    for c in children[header_idx + 1:end]:
        if getattr(c, "name", None) == "table":
            continue
        t = clean(c.get_text()) if hasattr(c, "get_text") else ""
        if t:
            texts.append(t)

    # Название могло переноситься визуально на следующий параграф (не
    # похожий на служебный заголовок и не на начало нового §) — в HTML
    # такое не встретилось (заголовок всегда один параграф), но на
    # всякий случай не заглатываем текст дальше первого совпадения.
    i = 0

    rest = " ".join(texts[i:])
    unit_m = UNIT_RE.search(rest)
    unit_phrase = clean(unit_m.group(1)) if unit_m else None

    crew_fragments = []
    j = i
    while j < len(texts) - 1:
        if texts[j] == "Состав" and texts[j + 1] == "звена":
            k = j + 2
            frag = []
            while k < len(texts) and texts[k] not in STOP_HEADERS:
                frag.append(texts[k])
                k += 1
            f = clean(" ".join(frag))
            if f:
                crew_fragments.append(f)
            j = k
        else:
            j += 1
    crew_raw = " | ".join(crew_fragments) if crew_fragments else None

    return {"code": code, "title": title, "unit_phrase": unit_phrase, "crew_raw": crew_raw}


def table_to_grid(table):
    """<table> -> список строк, каждая строка — список (текст, col_start,
    col_span). col_start учитывает colspan предыдущих ячеек той же
    строки (rowspan не разворачиваем — в проверенных образцах не
    встретился ни разу, но не рушимся, если попадётся: просто не
    расширяем по вертикали)."""
    grid = []
    for tr in table.find_all("tr"):
        row = []
        col = 0
        for td in tr.find_all(["td", "th"]):
            span = int(td.get("colspan") or 1)
            row.append((clean(td.get_text(" ")), col, span))
            col += span
        grid.append(row)
    return grid


def is_legend_row(row):
    """Строка-подпись вида 'а б в г ... №' в конце таблицы — не данные."""
    texts = [t for t, _, _ in row if t]
    if not texts:
        return False
    return all(t.lower() in LETTER_LEGEND or t in LETTER_LEGEND for t in texts)


def row_is_data(row):
    """Строка данных — содержит хотя бы одну ячейку в формате
    'Н.вр Расц.' (пара) или явную подпись 'Н.вр.'. Header-строки этому
    не удовлетворяют — используется, чтобы не принять данные ЗА
    заголовок соседнего столбца (см. находку на Е5-1-1/Е26-16: без этой
    границы заголовком колонки ошибочно становились числа из ДРУГОЙ,
    уже обработанной строки данных)."""
    for t, c, s in row:
        if PAIR_RE.match(t) or PAIR_MACH_RE.match(t):
            return True
        if t.strip("., ").lower() in ("н.вр", "нвр"):
            return True
    return False


def first_data_row_index(grid):
    for i, row in enumerate(grid):
        if not is_legend_row(row) and row_is_data(row):
            return i
    return len(grid)


def leftmost_labels(grid, limit):
    """Значение самой левой (col_start==0) ячейки в каждой из первых
    `limit` строк, с переносом значения вниз при пустой ячейке (типовой
    приём ЕНиР — подпись не повторяется на каждой строке, действует до
    следующего явного значения)."""
    labels = [None] * limit
    current = None
    for i in range(limit):
        row = grid[i]
        cell = next((t for t, c, s in row if c == 0), "")
        if cell:
            current = cell
        labels[i] = current
    return labels


def col_header(grid, header_row_limit, col_start):
    """Собрать заголовок колонки ТОЛЬКО из настоящих header-строк (индекс
    < header_row_limit, т.е. до первой строки данных) — не заглядывать
    в строки данных, иначе заголовком колонки ошибочно становятся числа
    соседней уже обработанной строки (см. row_is_data)."""
    labels = []
    for r in range(header_row_limit - 1, -1, -1):
        row = grid[r]
        if is_legend_row(row):
            continue
        matched = None
        for t, c, s in row:
            if c <= col_start < c + s and t:
                matched = t
                break
        if matched:
            labels.append(matched)
    labels.reverse()
    dedup = []
    for l in labels:
        if not dedup or dedup[-1] != l:
            dedup.append(l)
    return dedup


def extract_from_table(grid):
    """Вернуть список {condition, hours} из одной таблицы, либо [] если
    в ней не нашлось ни одного надёжно опознанного значения Н.вр."""
    header_limit = first_data_row_index(grid)
    left_labels = leftmost_labels(grid, header_limit) if header_limit else []
    # На строки данных тоже переносим leftmost-подпись (со сдвигом
    # индекса) — тот же приём "ditto", но уже среди самих строк данных
    # (напр. Е5-1-1: "Вручную" стоит только на своей строке, "Краном"
    # относится и к следующей строке с Машинистом).
    data_left = [None] * len(grid)
    current = None
    for i, row in enumerate(grid):
        cell = next((t for t, c, s in row if c == 0), "")
        if cell:
            current = cell
        data_left[i] = current

    results = []
    for r_idx, row in enumerate(grid):
        if is_legend_row(row) or r_idx < header_limit:
            continue
        label = data_left[r_idx]
        for text, col_start, span in row:
            hours = None
            m = PAIR_RE.match(text)
            if m:
                hours = m.group(1)
            else:
                m2 = PAIR_MACH_RE.match(text)
                if m2:
                    hours = m2.group(1)
            if hours is None:
                continue
            headers = [h for h in col_header(grid, header_limit, col_start)
                       if not h.lower().replace(" ", "").startswith(("н.вр", "нвр"))]
            condition_parts = [p for p in ([label] if label else []) + headers if p]
            results.append({
                "condition": "; ".join(dict.fromkeys(condition_parts)) or None,
                "hours_per_unit": hours.replace(",", "."),
            })

    if results:
        return results

    # Вторая ветка: явная строка "Н.вр." (без пары с расценкой в одной
    # ячейке, формат Е22) — берём числа справа от неё, заголовок колонок
    # ищем только среди настоящих header-строк (до этой самой строки —
    # она сама и есть первая строка данных, header_limit уже это учтёт).
    for r_idx, row in enumerate(grid):
        if r_idx < header_limit:
            continue
        label_col = None
        for text, col_start, span in row:
            if text.strip("., ").lower() in ("н.вр", "нвр"):
                label_col = col_start + span
                break
        if label_col is None:
            continue
        last_col_start = max((c for _, c, _ in row), default=-1)
        for text, col_start, span in row:
            if col_start < label_col or col_start == last_col_start:
                continue
            if not BARE_NUM_RE.match(text):
                continue
            headers = [h for h in col_header(grid, header_limit, col_start)
                       if not h.lower().replace(" ", "").startswith(("н.вр", "нвр"))]
            results.append({
                "condition": "; ".join(dict.fromkeys(headers)) or None,
                "hours_per_unit": text.replace(",", "."),
            })
    return results


def main():
    conv_dir = Path("/home/oleg/Documents/TM-35/import/enir_work/conv")
    out_path = Path("/home/oleg/Documents/TM-35/import/enir_work/enir_norms_v2.json")

    all_rows = []
    stats = []
    for html_path in sorted(conv_dir.glob("*.html")):
        sbornik = html_path.stem
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        body = soup.body
        children = list(body.children)
        children = [c for c in children if getattr(c, "name", None) is not None]

        header_idxs = find_headers(children)
        n_paragraphs = 0
        n_with_hours = 0
        for k, h in enumerate(header_idxs):
            end = header_idxs[k + 1] if k + 1 < len(header_idxs) else len(children)
            meta = parse_header_and_crew(children, h, end)
            if not meta:
                continue
            n_paragraphs += 1

            tables = [c for c in children[h + 1:end] if getattr(c, "name", None) == "table"]
            para_rows = []
            for t in tables:
                grid = table_to_grid(t)
                para_rows.extend(extract_from_table(grid))

            if para_rows:
                n_with_hours += 1
                for pr in para_rows:
                    all_rows.append({
                        "sbornik": sbornik,
                        "code": meta["code"],
                        "title": meta["title"],
                        "unit_phrase": meta["unit_phrase"],
                        "crew_raw": meta["crew_raw"],
                        "condition": pr["condition"],
                        "hours_per_unit": pr["hours_per_unit"],
                        "parsed": True,
                    })
            else:
                all_rows.append({
                    "sbornik": sbornik,
                    "code": meta["code"],
                    "title": meta["title"],
                    "unit_phrase": meta["unit_phrase"],
                    "crew_raw": meta["crew_raw"],
                    "condition": None,
                    "hours_per_unit": None,
                    "parsed": False,
                })

        stats.append((sbornik, n_paragraphs, n_with_hours))

    out_path.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1), encoding="utf-8")

    total_paragraphs = sum(s[1] for s in stats)
    total_with_hours = sum(s[2] for s in stats)
    print(f"{'Сборник':50s} {'§ найдено':>10s} {'с нормой':>10s}")
    for name, n, wh in stats:
        print(f"{name:50s} {n:>10d} {wh:>10d}")
    print(f"\nВсего §: {total_paragraphs}, с извлечённой нормой: {total_with_hours} "
          f"({100*total_with_hours/total_paragraphs:.1f}%)")
    print(f"Всего строк (с разложением по параметрам): {len(all_rows)}")


if __name__ == "__main__":
    main()
