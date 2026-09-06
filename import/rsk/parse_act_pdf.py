#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разбор акта проверки РСК (PDF, координатный метод) в parsed_act.json.
Ничего не пишет в БД.

pdfplumber.extract_tables() не используется — даёт 79 позиций из 152,
теряя строки на переносах страниц (проверено). Разбор идёт по словам
(extract_words) и их координатам: границы колонок постоянны по всему
документу — x = 56.0 / 90.5 / 323.9 / 453.7 / 552.7 (ширина страницы
594.96): № п/п | Системный №, выявленное нарушение | Мероприятие по
устранению | Срок устранения.

**Привязка по линиям таблицы, не по диапазону между якорями.** Первая
версия резала текст колонок между позициями "№ NNN Повторно" по
вертикальному диапазону — эвристика, давшая систематические протечки на
12 из 152 позиций (№17, 27, 45, 47, 126, 127, 375, 380-383, 387): ячейка
одной физической строки таблицы не всегда одной высоты в разных
колонках, и диапазон, вычисленный по колонке 1, для соседних колонок
иногда обрезал начало/конец не там. Настоящая граница ячеек — это
горизонтальные линии в `page.rects` (`height < 2`, как и вертикальные
разделители колонок — `width < 2`, x = те же 56.0/90.5/…). Разбор ниже
строит по ним полосы (по одной на строку таблицы, полос 418 на весь
документ), относит каждое слово к ячейке (полоса × колонка) и явно
дописывает полосы-продолжения к позиции, чей якорь их открыл — вместо
того чтобы гадать по вертикальному промежутку.

Якорь позиции — ячейка колонки 1 (0-индексация: № п/п=0, текст
нарушения=1, мероприятие=2, срок=3), начинающаяся с "№ NNN" и такая, что
СЛЕДУЮЩЕЕ слово начинается с "Повторно" (проверено на всех 152
нарушениях акта 4183-159 — ни одного НЕ повторного нет; это надёжнее,
чем полагаться на то, что "№ NNN" стоит первым словом строки, потому что
"№" встречается и как ссылка на другой документ/закон/номер столбца
журнала посреди чужого нарушения, напр. "№ 2 (Наименование бетонируемой…",
"№ 883н «Об утверждении правил…" — оба варианта отфильтрованы этим
правилом естественно, без отдельных доп. эвристик).

Заголовки 4 контрольных мероприятий ("При проверке… установлено
следующее:") — не аккуратные строки одной колонки, а сплошной абзац,
физически перетекающий через границы столбцов 1-3 (проверено: слова
одной физической строки лежат на x0 от 96.9 до 482.6). Заголовочные
полосы исключаются по границам "При …" → "…следующее:" (per-page,
единственная надёжная пара текстовых маркеров), а не по геометрии.
"""
import json
import re
import sys
from collections import defaultdict

import pdfplumber

PDF_PATH = "/home/oleg/Documents/TM-35/Акт проверки № 4183-159 от 11.08.2026 (1).pdf"
OUT_PATH = "/home/oleg/Documents/TM-35/import/rsk/parsed_act.json"

ACT_NO = "4183-159"
ACT_DATE = "2026-08-11"

BOUNDS = [56.0, 90.5, 323.9, 453.7, 552.7]
TOL = 2.0
HEADER_TOP_MAX = 25
FOOTER_TOP_MIN = 800


def col_of(x0):
    for i in range(4):
        if BOUNDS[i] - TOL <= x0 < BOUNDS[i + 1] + TOL:
            return i
    return None


# =======================================================================
# Полосы (строки таблицы) — по горизонтальным линиям page.rects.
# =======================================================================

def page_band_boundaries(page):
    """[0.0] + горизонтальные линии страницы + [высота страницы]. Ноль в
    начале обязателен — строки перетекают с предыдущей страницы, первая
    полоса страницы может быть продолжением позиции с прошлой."""
    horiz = sorted(set(round(r["top"], 1) for r in page.rects if r["height"] < 2))
    return [0.0] + horiz + [page.height]


def band_index(boundaries, top):
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= top < boundaries[i + 1]:
            return i
    return len(boundaries) - 2  # верхняя граница включительно на самом низу страницы


FOOTER_RE = re.compile(r"^Акт проверки от \d{2}\.\d{2}\.\d{4}\s*№\s*4183-159$")
PAGENO_RE = re.compile(r"^\d+$")


def load_bands():
    """Возвращает список полос в порядке чтения документа:
    [{"page", "band", "cells": {0: "...", 1: "...", 2: "...", 3: "..."}}]
    — уже без колонтитулов (по содержимому: строка-колонтитул целиком,
    строка из одних цифр — номер страницы) и без текста последней
    страницы после "Общее количество нарушений"."""
    bands_out = []
    total_declared = None
    raw_band_count = 0
    with pdfplumber.open(PDF_PATH) as pdf:
        for pi, page in enumerate(pdf.pages):
            raw_band_count += len(page_band_boundaries(page)) - 1
            raw = page.extract_words()
            cutoff = None
            for w in raw:
                if w["text"] == "Общее" and any(
                    w2["text"] == "количество" and abs(w2["top"] - w["top"]) < 3 for w2 in raw
                ):
                    cutoff = w["top"]
                    break
            if cutoff is not None:
                m = re.search(r"нарушений\s*-\s*(\d+)", page.extract_text() or "")
                if m:
                    total_declared = int(m.group(1))

            words = []
            for w in raw:
                top = w["top"]
                if top < HEADER_TOP_MAX or top > FOOTER_TOP_MIN:
                    continue
                if cutoff is not None and top >= cutoff:
                    continue
                c = col_of(w["x0"])
                if c is None:
                    continue
                words.append({"top": top, "x0": w["x0"], "text": w["text"], "col": c})

            boundaries = page_band_boundaries(page)
            per_band = defaultdict(lambda: defaultdict(list))  # band_idx -> col -> [words]
            for w in words:
                bi = band_index(boundaries, w["top"])
                per_band[bi][w["col"]].append(w)

            for bi in sorted(per_band.keys()):
                cells = {}
                for col in range(4):
                    ws = per_band[bi].get(col, [])
                    if not ws:
                        cells[col] = None
                        continue
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    text = clean_join([w["text"] for w in ws], word_level=True)
                    cells[col] = text

                # Колонтитул — вся полоса состоит только из этого текста
                # (обычно одна колонка, но на случай если границы линий
                # чуть сдвинули слова между колонками — проверяем весь
                # непустой текст полосы).
                nonempty = [c for c in cells.values() if c]
                joined = " ".join(nonempty)
                if FOOTER_RE.match(joined.strip()):
                    continue
                if len(nonempty) == 1 and PAGENO_RE.match(nonempty[0].strip()):
                    continue
                if not nonempty:
                    continue

                bands_out.append({"page": pi, "band": bi, "cells": cells})

    return bands_out, total_declared, raw_band_count


def clean_join(tokens, word_level=False):
    """word_level=True — склейка отдельных слов пробелом (внутри ячейки,
    шаг 4 докса), с обработкой переноса через дефис на конце СЛОВА перед
    словом с маленькой буквы (`материа-` + `лы` -> `материалы`).

    Важно: маркер списка "-" как ОТДЕЛЬНОЕ слово (`- снятие плодородного
    слоя…`) — не перенос, трогать нельзя. Отличается от переноса длиной
    токена: у переноса дефис приклеен к остатку слова (`материа-`, длина
    > 1), у маркера списка токен — это сам по себе одиночный "-"
    (проверено на 73 записях: наивная проверка "текст оканчивается на -"
    рвала маркеры списков, склеивая "- снятие" в "снятие" без дефиса)."""
    if not tokens:
        return None
    if not word_level:
        out = tokens[0]
        for part in tokens[1:]:
            if out.endswith("-") and part and part[0].islower():
                out = out[:-1] + part
            else:
                out = out + " " + part
        return re.sub(r"\s+", " ", out).strip() or None

    out = tokens[0]
    prev_token = tokens[0]
    for token in tokens[1:]:
        if prev_token.endswith("-") and len(prev_token) > 1 and token and token[0].islower():
            out = out[:-1] + token
        else:
            out = out + " " + token
        prev_token = token
    out = re.sub(r"\s+", " ", out).strip()
    return out or None


# =======================================================================
# Заголовки контрольных мероприятий — исключаются по тексту ячеек полос,
# та же пара маркеров "При …" → "…следующее:", что и в первой версии.
# =======================================================================

def find_header_band_range(bands):
    """Индексы (в списке bands, по порядку чтения) полос-заголовков —
    от полосы с "При " в начале ячейки 1 до полосы с "…следующее:" ГДЕ-ТО
    В ЛЮБОЙ ячейке (сплошной абзац заголовка перетекает через границы
    колонок — последнее слово "следующее:" у двух из четырёх заголовков
    физически попадает в x0 колонки 2, а не колонки 1: проверено,
    x0=347.09 при границе колонки 2 с 323.9), включительно, на той же
    странице, в пределах 3 полос (абзац из 2-3 строк)."""
    header_band_ids = set()
    starts = []
    for i, b in enumerate(bands):
        c1 = b["cells"].get(1) or ""
        if c1.startswith("При "):
            starts.append(i)
    for i, b in enumerate(bands):
        full = " ".join(v for v in b["cells"].values() if v)
        if "следующее:" in full:
            candidates = [s for s in starts if s <= i and bands[s]["page"] == b["page"] and (i - s) <= 3]
            if not candidates:
                continue
            s = max(candidates)
            for k in range(s, i + 1):
                header_band_ids.add(k)
    return header_band_ids


# =======================================================================
# Якоря позиций — по ячейке 1 полосы.
# =======================================================================

ANCHOR_RE = re.compile(r"^№\s*(\d+)\s+(Повторно\b.*)$")


def anchor_sys_no(cell_text):
    if not cell_text:
        return None
    m = ANCHOR_RE.match(cell_text)
    if not m:
        return None
    return int(m.group(1))


def to_iso(dmy):
    d, m, y = dmy.split(".")
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


ITEM_NO_RE = re.compile(r"\b(\d+)\.(\d+)\b")
DUE_BLOCK_RE = re.compile(
    r"Выявлено\s+при\s+проведении\s+проверки\s+(\d{1,2}\.\d{1,2}\.\d{4})\s*№\s*(4183-\d+)\.?"
    r"\s*Устранить\s+до\s+(\d{1,2}\.\d{1,2}\.\d{4})"
)


def main():
    bands, total_declared, raw_band_count = load_bands()
    header_band_ids = find_header_band_range(bands)
    n_header_zones = sum(
        1 for i in sorted(header_band_ids)
        if i - 1 not in header_band_ids
    )
    if n_header_zones != 4:
        print(f"!! ОСТАНОВКА: найдено {n_header_zones} заголовков контрольных мероприятий, ожидалось 4",
              file=sys.stderr)

    content_bands = [b for i, b in enumerate(bands) if i not in header_band_ids and b["page"] > 0]

    # Индексы полос-якорей внутри content_bands.
    anchor_positions = []
    for i, b in enumerate(content_bands):
        sys_no = anchor_sys_no(b["cells"].get(1))
        if sys_no is not None:
            anchor_positions.append((i, sys_no))

    issues = []
    records = []
    m = len(anchor_positions)
    for k, (band_i, sys_no) in enumerate(anchor_positions):
        end_i = anchor_positions[k + 1][0] if k + 1 < m else len(content_bands)
        group = content_bands[band_i:end_i]

        def col_text(col):
            parts = [b["cells"].get(col) for b in group]
            parts = [p for p in parts if p]
            return clean_join(parts) if parts else None

        content = col_text(1)
        if content:
            content = re.sub(r"^№\s*\d+\s*Повторно\.?\s*", "", content).strip() or None

        remedy = col_text(2)

        col3_text = col_text(3) or ""
        due_m = DUE_BLOCK_RE.search(col3_text)
        if due_m:
            first_date = to_iso(due_m.group(1))
            first_act = due_m.group(2)
            due_date = to_iso(due_m.group(3))
        else:
            first_date = first_act = due_date = None

        col0_text = col_text(0) or ""
        item_m = ITEM_NO_RE.search(col0_text)
        item_no = f"{item_m.group(1)}.{item_m.group(2)}" if item_m else None
        control_section = int(item_m.group(1)) if item_m else None

        records.append({
            "sys_no": sys_no, "item_no": item_no, "control_section": control_section,
            "content": content, "remedy": remedy,
            "due_date": due_date, "first_detected_date": first_date, "first_act_no": first_act,
            "is_repeat": True,  # см. докстринг модуля — подтверждено для всех 152
        })

    for r in records:
        if not r["remedy"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_remedy", "message": "Пустая колонка «Мероприятие»"})
        if not r["due_date"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_due_date", "message": "Не найден «Устранить до»"})
        if not r["first_act_no"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_first_act",
                            "message": "Не найдено «Выявлено при проведении проверки… №4183-XX»"})
        if not r["item_no"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_item_no", "message": "Не найден № п/п (N.M)"})

    sysnos = [r["sys_no"] for r in records]
    checks = {
        "bands_total": raw_band_count,
        "bands_with_anchor": len(anchor_positions),
        "unique_sys_no": len(set(sysnos)),
        "total_declared_in_act": total_declared,
        "no_remedy": sum(1 for i in issues if i["kind"] == "no_remedy"),
        "no_due_date": sum(1 for i in issues if i["kind"] == "no_due_date"),
        "no_first_act": sum(1 for i in issues if i["kind"] == "no_first_act"),
        "no_item_no": sum(1 for i in issues if i["kind"] == "no_item_no"),
        "control_sections_found": n_header_zones,
    }

    out = {
        "act": {"act_no": ACT_NO, "act_date": ACT_DATE, "total_declared": total_declared,
                "pdf_path": PDF_PATH},
        "records": records,
        "issues": issues,
        "checks": checks,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print("Записано:", OUT_PATH)


if __name__ == "__main__":
    main()
