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

Якорь позиции — строка колонки 2, начинающаяся с "№ NNN" и такая, что
СЛЕДУЮЩЕЕ слово в этой же строке начинается с "Повторно" (проверено на
всех 152 нарушениях акта 4183-159 — ни одного НЕ повторного нет; это
надёжнее, чем полагаться на то, что "№ NNN" стоит первым словом строки,
потому что "№" встречается и как ссылка на другой документ/закон/номер
столбца журнала посреди чужого нарушения, напр. "№ 2 (Наименование
бетонируемой…", "№ 883н «Об утверждении правил…" — оба варианта
отфильтрованы этим правилом естественно, без отдельных доп. эвристик).

Заголовки 4 контрольных мероприятий ("При проверке… установлено
следующее:") — не аккуратные строки одной колонки, а сплошной абзац,
физически перетекающий через границы столбцов 1-3 (проверено: слова
одной физической строки лежат на x0 от 96.9 до 482.6 — это подтверждает,
что использовать "ширину строки" как признак заголовка нельзя: у обычных
строк тоже бывает, что колонки 1 и 2 стартуют на одной высоте). Поэтому
заголовки исключаются по границам "При …" → "…следующее:" (per-page,
единственная надёжная пара маркеров), а не по геометрии.
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


def load_words():
    """Все слова документа (page, top, x0, text), без колонтитулов и без
    текста последней страницы после "Общее количество" (правило 5)."""
    words = []
    with pdfplumber.open(PDF_PATH) as pdf:
        total_declared = None
        for pi, page in enumerate(pdf.pages):
            raw = page.extract_words()
            cutoff = None
            for w in raw:
                if w["text"] == "Общее" and any(
                    w2["text"] == "количество" and abs(w2["top"] - w["top"]) < 3 for w2 in raw
                ):
                    cutoff = w["top"]
                    break
            if cutoff is not None:
                # "Общее количество нарушений - 152" — число сразу после тире на той же строке.
                m = re.search(r"нарушений\s*-\s*(\d+)", page.extract_text() or "")
                if m:
                    total_declared = int(m.group(1))
            for w in raw:
                top = w["top"]
                if top < HEADER_TOP_MAX or top > FOOTER_TOP_MIN:
                    continue
                if cutoff is not None and top >= cutoff:
                    continue
                words.append({"page": pi, "top": top, "x0": w["x0"], "text": w["text"]})
    return words, total_declared


def find_header_zones(words):
    """4 заголовка контрольных мероприятий — от "При …" (первое слово
    строки, левый край колонки 1) до ближайшего последующего "…следующее:"
    на той же странице в пределах 80pt (эмпирически: абзац из 3 строк,
    ~13.5pt между строками)."""
    starts = [(w["page"], w["top"]) for w in words if w["text"] == "При" and w["x0"] < 100]
    ends = [(w["page"], w["top"]) for w in words if "следующее" in w["text"]]
    zones = []
    for ep, et in ends:
        candidates = [(sp, st) for sp, st in starts if sp == ep and st < et and (et - st) < 80]
        if not candidates:
            continue
        sp, st = max(candidates, key=lambda c: c[1])
        zones.append((ep, st - 1, et + 1))
    zones.sort(key=lambda z: (z[0], z[1]))
    return zones


def in_any_zone(zones, page, top):
    return any(page == zp and zs <= top <= ze for zp, zs, ze in zones)


def section_of(zones, page, top):
    """control_section — порядковый номер зоны-заголовка, ПОСЛЕ которой
    расположено нарушение (зоны идут по возрастанию позиции в документе)."""
    n = 0
    for zp, zs, ze in zones:
        if (page, top) > (zp, ze):
            n += 1
        else:
            break
    return n or None


def group_lines(words):
    lines = defaultdict(list)
    for w in words:
        lines[(w["page"], round(w["top"], 1))].append(w)
    for k in lines:
        lines[k].sort(key=lambda w: w["x0"])
    return lines


def find_anchors(words, zones):
    """Возвращает список (page, top, sys_no) — по одному на каждое
    нарушение, отсортировано в порядке чтения документа."""
    col1 = [w for w in words if col_of(w["x0"]) == 1 and w["page"] > 0
            and not in_any_zone(zones, w["page"], w["top"])]
    lines = group_lines(col1)
    anchors = []
    for (pi, top), ws in lines.items():
        first = ws[0]["text"]
        if first == "№":
            restword = ws[1]["text"] if len(ws) > 1 else ""
            next_word = ws[2]["text"] if len(ws) > 2 else ""
        elif first.startswith("№") and len(first) > 1 and first[1:2].isdigit():
            restword = first[1:]
            next_word = ws[1]["text"] if len(ws) > 1 else ""
        else:
            continue
        m = re.match(r"^(\d+)", restword)
        if not m:
            continue
        if not next_word.startswith("Повторно"):
            continue
        # Настоящий (не округлённый) top — иначе сравнение с сырыми top
        # слов колонок 2/3 при нарезке по диапазону теряет слова, чей top
        # на доли пункта меньше округлённого (напр. "Выявлено при" на той
        # же строке, что и якорь, top=737.18 против округлённого 737.2).
        real_top = min(w["top"] for w in ws)
        anchors.append((pi, real_top, int(m.group(1))))
    anchors.sort(key=lambda a: (a[0], a[1]))
    return anchors


def pos_key(page, top):
    return (page, top)


def slice_column_text(words, col_idx, zones, start, end_exclusive):
    """Все слова колонки col_idx между двумя позициями документа
    [start, end_exclusive), исключая заголовочные зоны, собранные построчно
    (пробел между словами одной строки, перевод строки между строками)."""
    ws = [
        w for w in words
        if col_of(w["x0"]) == col_idx
        and start <= pos_key(w["page"], w["top"]) < end_exclusive
        and not in_any_zone(zones, w["page"], w["top"])
    ]
    lines = group_lines(ws)
    ordered_keys = sorted(lines.keys())
    parts = []
    for k in ordered_keys:
        parts.append(" ".join(w["text"] for w in lines[k]))
    return clean_join(parts)


def clean_join(line_parts):
    """Склейка строк одной ячейки в абзац: перенос слова через дефис на
    конце строки — без пробела и без дефиса; иначе — пробел."""
    if not line_parts:
        return None
    out = line_parts[0]
    for part in line_parts[1:]:
        if out.endswith("-") and part and part[0].islower():
            out = out[:-1] + part
        else:
            out = out + " " + part
    out = re.sub(r"\s+", " ", out).strip()
    return out or None


def to_iso(dmy):
    d, m, y = dmy.split(".")
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


ITEM_NO_RE = re.compile(r"^\d+\.\d+$")


def find_item_nos(words, zones):
    """№ п/п (колонка 0, 'N.M') для всех 152 позиций, в порядке чтения
    документа. Сопоставляется с якорями ПО ПОРЯДКОВОМУ НОМЕРУ, не по
    диапазону позиций — та же вертикальная нестыковка ячеек одной строки,
    что и у колонок 2/3 (см. find_due_blocks), а вдобавок сам № п/п в
    источнике не уникален: "2.13" и "2.14" в акте буквально повторяются
    по 4 раза подряд для РАЗНЫХ нарушений (проверено по координатам —
    это опечатка/копипаст самого акта, не баг разбора). Из-за этого
    диапазонная привязка по позиции для части записей давала один и тот
    же item_no на несколько разных sys_no; порядковая привязка ("i-й
    номер в колонке 0 соответствует i-му по счёту нарушению") работает
    корректно независимо от дублей текста — количество токенов (152)
    совпадает с числом нарушений и идёт в том же порядке."""
    col0 = [w for w in words if col_of(w["x0"]) == 0 and w["page"] > 0
            and not in_any_zone(zones, w["page"], w["top"]) and ITEM_NO_RE.match(w["text"])]
    col0.sort(key=lambda w: pos_key(w["page"], w["top"]))
    return [w["text"] for w in col0]


def find_due_blocks(words, zones):
    """Блоки "Выявлено при проведении проверки ДД.ММ.ГГГГ №4183-XX…
    Устранить до ДД.ММ.ГГГГ" колонки 3, в порядке чтения документа —
    ОТДЕЛЬНО от нарезки по диапазону между якорями колонки 1. Нужно: для
    2 из 152 позиций (129, 17) этот блок в колонке 3 начинается на ~1
    строку ВЫШЕ, чем строка якоря в колонке 1 (разная вертикальная
    привязка ячеек одной строки таблицы у разных колонок — не опечатка и
    не баг конкретной позиции, поэтому не резать по границе якоря).
    Блоков ровно 152, порядок совпадает с порядком якорей (проверено) —
    сопоставляем по порядковому номеру, не по диапазону позиций."""
    col3 = [w for w in words if col_of(w["x0"]) == 3 and w["page"] > 0
            and not in_any_zone(zones, w["page"], w["top"])]
    lines = group_lines(col3)
    ordered_keys = sorted(lines.keys())
    full_text = clean_join([" ".join(w["text"] for w in lines[k]) for k in ordered_keys])
    pattern = re.compile(
        r"Выявлено\s+при\s+проведении\s+проверки\s+(\d{1,2}\.\d{1,2}\.\d{4})\s*№\s*(4183-\d+)\.?"
        r"\s*Устранить\s+до\s+(\d{1,2}\.\d{1,2}\.\d{4})"
    )
    return [(to_iso(m.group(1)), m.group(2), to_iso(m.group(3))) for m in pattern.finditer(full_text or "")]


def fix_remedy_spillover(records):
    """Колонка 2 (Мероприятие), в отличие от колонки 3, не имеет своего
    надёжного маркера начала строки ("Выявлено…") — высота её ячейки не
    всегда совпадает с высотой ячейки колонки 1 той же строки, и НАЧАЛО
    мероприятия позиции i+1 иногда физически печатается ВЫШЕ, чем якорь
    i+1 в колонке 1 — из-за чего диапазонная нарезка [start_i, start_{i+1})
    захватывает это чужое начало в ХВОСТ позиции i (проверено на 3 из
    152: №17→27, №387→375, №381→382 — во всех трёх хвост «съедал» первое
    слово/слова СЛЕДУЮЩЕГО мероприятия, а не было собственной обрезанной
    концовкой текущего: первая проверка была наоборот, ошибочно
    ДОБАВЛЯЛА текст следующей позиции в текущую вместо того чтобы отдать
    лишнее туда, куда оно реально относится — поймано сверкой by hand
    на этих трёх записях, не поверено на слово).

    Признак обрыва — remedy не заканчивается точкой. Хвост ПОСЛЕ
    последней точки — на самом деле начало мероприятия следующей
    позиции: переносим его туда (в начало), а не тянем данные оттуда
    сюда."""
    for i in range(len(records) - 1):
        cur = records[i]
        nxt = records[i + 1]
        rem = cur["remedy"]
        if not rem or rem.rstrip().endswith((".", "!", "?", ":")):
            continue
        last_dot = rem.rfind(".")
        if last_dot == -1:
            # Ни одной точки вообще — не обязательно межколоночная утечка,
            # может быть просто мероприятие без финальной точки в самом
            # источнике (напр. №6 — цельное по смыслу предложение без
            # точки на конце). Трогать нечего: не с чем сравнивать, что
            # именно "хвост", а что — всё предложение целиком.
            continue
        true_rem = rem[: last_dot + 1].strip()
        leaked = rem[last_dot + 1:].strip()
        if not leaked:
            continue
        cur["remedy"] = true_rem
        nxt["remedy"] = (leaked + " " + (nxt["remedy"] or "")).strip() or None


def main():
    words, total_declared = load_words()
    zones = find_header_zones(words)
    if len(zones) != 4:
        print(f"!! ОСТАНОВКА: найдено {len(zones)} заголовков контрольных мероприятий, ожидалось 4",
              file=sys.stderr)
    anchors = find_anchors(words, zones)
    due_blocks = find_due_blocks(words, zones)
    if len(due_blocks) != len(anchors):
        print(f"!! ОСТАНОВКА: {len(due_blocks)} блоков срока устранения против {len(anchors)} якорей",
              file=sys.stderr)
    item_nos = find_item_nos(words, zones)
    if len(item_nos) != len(anchors):
        print(f"!! ОСТАНОВКА: {len(item_nos)} номеров № п/п против {len(anchors)} якорей", file=sys.stderr)

    issues = []
    records = []
    n = len(anchors)
    for i, (page, top, sys_no) in enumerate(anchors):
        start = pos_key(page, top)
        end = pos_key(*anchors[i + 1][:2]) if i + 1 < n else (10 ** 9, 0)

        content = slice_column_text(words, 1, zones, start, end)
        # "№ NNN Повторно." — срезать префикс, он не часть содержания.
        if content:
            content = re.sub(r"^№\s*\d+\s*Повторно\.?\s*", "", content).strip()

        remedy = slice_column_text(words, 2, zones, start, end)
        first_date, first_act, due_date = due_blocks[i] if i < len(due_blocks) else (None, None, None)
        item_no = item_nos[i] if i < len(item_nos) else None
        control_section = int(item_no.split(".")[0]) if item_no else section_of(zones, page, top)

        records.append({
            "sys_no": sys_no, "item_no": item_no, "control_section": control_section,
            "content": content, "remedy": remedy,
            "due_date": due_date, "first_detected_date": first_date, "first_act_no": first_act,
            "is_repeat": True,  # см. докстринг модуля — подтверждено для всех 152
        })

    fix_remedy_spillover(records)

    # Отчёт сверки — по ИТОГОВОМУ состоянию записей (после fix_remedy_spillover),
    # не по промежуточному до правки хвостов между колонками.
    for r in records:
        if not r["remedy"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_remedy", "message": "Пустая колонка «Мероприятие»"})
        if not r["due_date"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_due_date", "message": "Не найден «Устранить до»",
                            "raw_value": None})
        if not r["first_act_no"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_first_act",
                            "message": "Не найдено «Выявлено при проведении проверки… №4183-XX»",
                            "raw_value": None})
        if not r["item_no"]:
            issues.append({"sys_no": r["sys_no"], "kind": "no_item_no", "message": "Не найден № п/п (N.M)"})

    sysnos = [r["sys_no"] for r in records]
    checks = {
        "anchors_found": len(records),
        "unique_sys_no": len(set(sysnos)),
        "total_declared_in_act": total_declared,
        "no_remedy": sum(1 for i in issues if i["kind"] == "no_remedy"),
        "no_due_date": sum(1 for i in issues if i["kind"] == "no_due_date"),
        "no_first_act": sum(1 for i in issues if i["kind"] == "no_first_act"),
        "no_item_no": sum(1 for i in issues if i["kind"] == "no_item_no"),
        "control_sections_found": len(zones),
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
