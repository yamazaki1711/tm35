# -*- coding: utf-8 -*-
"""
Разбор акта проверки РСК (PDF, координатный метод) — общий модуль для
CLI-скрипта (import/rsk/parse_act_pdf.py, там же полная история находок
в докстринге) и формы «Загрузка акта проверки» (main.py). Ничего не
пишет в БД и не трогает файловую систему, кроме чтения pdf_path.

Метод — по линиям таблицы (page.rects), не по диапазону между якорями;
подробное обоснование каждого решения — в
docs/RSK_ACT_PDF_IMPORT_2026-09-06.md и в import/rsk/parse_act_pdf.py.
"""
import re
from collections import defaultdict

import pdfplumber

BOUNDS = [56.0, 90.5, 323.9, 453.7, 552.7]
TOL = 2.0
HEADER_TOP_MAX = 25
FOOTER_TOP_MIN = 800

ACT_HEADER_RE = re.compile(r"АКТ ПРОВЕРКИ №\s*(4183-\d+)")
ACT_DATE_RE = re.compile(r"«(\d{1,2})»\s+(\S+)\s+(\d{4})\s*г\.")
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def col_of(x0):
    for i in range(4):
        if BOUNDS[i] - TOL <= x0 < BOUNDS[i + 1] + TOL:
            return i
    return None


def page_band_boundaries(page):
    horiz = sorted(set(round(r["top"], 1) for r in page.rects if r["height"] < 2))
    return [0.0] + horiz + [page.height]


def band_index(boundaries, top):
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= top < boundaries[i + 1]:
            return i
    return len(boundaries) - 2


FOOTER_RE = re.compile(r"^Акт проверки от \d{2}\.\d{2}\.\d{4}\s*№\s*4183-\d+$")
PAGENO_RE = re.compile(r"^\d+$")


def clean_join(tokens, word_level=False):
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


def load_bands(pdf_path):
    bands_out = []
    total_declared = None
    act_no = None
    act_date = None
    raw_band_count = 0
    with pdfplumber.open(pdf_path) as pdf:
        title_text = pdf.pages[0].extract_text() or ""
        m = ACT_HEADER_RE.search(title_text)
        if m:
            act_no = m.group(1)
        m = ACT_DATE_RE.search(title_text)
        if m:
            day, month_ru, year = m.groups()
            month = RU_MONTHS.get(month_ru.lower())
            if month:
                act_date = "%04d-%02d-%02d" % (int(year), month, int(day))

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
                m2 = re.search(r"нарушений\s*-\s*(\d+)", page.extract_text() or "")
                if m2:
                    total_declared = int(m2.group(1))

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
            per_band = defaultdict(lambda: defaultdict(list))
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
                    cells[col] = clean_join([w["text"] for w in ws], word_level=True)

                nonempty = [c for c in cells.values() if c]
                joined = " ".join(nonempty)
                if FOOTER_RE.match(joined.strip()):
                    continue
                if len(nonempty) == 1 and PAGENO_RE.match(nonempty[0].strip()):
                    continue
                if not nonempty:
                    continue

                bands_out.append({"page": pi, "band": bi, "cells": cells})

    return bands_out, total_declared, raw_band_count, act_no, act_date


def find_header_band_range(bands):
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


def parse_act(pdf_path):
    """Возвращает {"act": {...}, "records": [...], "issues": [...], "checks": {...}}."""
    bands, total_declared, raw_band_count, act_no, act_date = load_bands(pdf_path)
    header_band_ids = find_header_band_range(bands)
    n_header_zones = sum(1 for i in sorted(header_band_ids) if i - 1 not in header_band_ids)

    content_bands = [b for i, b in enumerate(bands) if i not in header_band_ids and b["page"] > 0]

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
            "is_repeat": True,
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

    return {
        "act": {"act_no": act_no, "act_date": act_date, "total_declared": total_declared},
        "records": records,
        "issues": issues,
        "checks": checks,
    }
