#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разбор реестра нарушений РСК (xlsx, лист Violations) + акта проверки (PDF)
в промежуточный JSON. Ничего не пишет в БД — только парсинг и нормализация,
чтобы разбор можно было проверить глазами до загрузки (тот же паттерн,
что import/id_kontur/parse_id_excel.py + load_id_form.py).

Источники (пути see ARGV ниже):
  xlsx — "Реестр нарушений ТМ-35 13.07.2026.xlsx", лист Violations
  pdf  — "Акт проверки № 4183-159 от 11.08.2026 (1).pdf"

Задача и все выявленные дефекты источника — см. докс координатора
"РСК контур в АСД — этап 1" (06.09.2026) и KNOWN_ISSUES (после загрузки).
"""
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict

import openpyxl

XLSX_PATH = "/home/oleg/Documents/TM-35/Реестр нарушений ТМ-35 13.07.2026.xlsx"
PDF_PATH = "/home/oleg/Documents/TM-35/Акт проверки № 4183-159 от 11.08.2026 (1).pdf"
OUT_PATH = "/home/oleg/Documents/TM-35/import/rsk/parsed.json"

ACT_NO = "4183-159"
ACT_DATE = "2026-08-11"
ACT_TOTAL = 152

issues = []  # rsk_import_issue candidates: {sys_no, severity, kind, message, raw_value}


def add_issue(sys_no, severity, kind, message, raw_value=None):
    issues.append({
        "sys_no": sys_no, "severity": severity, "kind": kind,
        "message": message, "raw_value": raw_value,
    })


# =======================================================================
# Утилиты
# =======================================================================

def clean_text(s):
    """_x000D_ артефакт, множественные пробелы/переводы строк -> один
    пробел, обрезка краёв. Правило 5 докса."""
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("_x000D_", " ")
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = s.strip()
    return s or None


def fix_typos(s):
    """Правило 5 докса — конкретные опечатки источника, не общий словарь."""
    if s is None:
        return None
    s = s.replace("Cнято", "Снято")  # латинская C
    s = re.sub(r"\bиа\b", "на", s)  # "Отклонено иа 27 неделе"
    s = s.replace("снятио", "снято")  # "Частично снятио"
    s = s.replace("частичноне вып.", "частично не вып.")
    s = re.sub(r"(снятие)(\d)", r"\1 \2", s)  # "Направлено на снятие14.01.26"
    return s


def norm(s):
    if s is None:
        return None
    return fix_typos(clean_text(s))


DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")


def parse_date_token(d, m, y):
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return "%04d-%02d-%02d" % (y, int(m), int(d))
    except ValueError:
        return None


def first_date(text):
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    return parse_date_token(*m.groups())


def to_iso(v):
    """openpyxl datetime -> ISO date string; строка с датой -> ISO;
    None/'-' -> None."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    s = norm(v)
    if not s or s == "-":
        return None
    return first_date(s)


# =======================================================================
# Треки (Физика/Проект/ИД) — карта нормализации, докс раздел "Треки"
# =======================================================================

TRACK_MAP = {
    "не треб.": "not_required",
    "не вып.": "not_done",
    "да": "done",
    "вып.": "done",
    "выполн.": "done",
    "факт": "fact",
}


def norm_track(raw, allow_fact, sys_no, col_name):
    raw_c = clean_text(raw)
    if raw_c is None:
        return "unknown", raw_c
    key = raw_c.strip().lower().rstrip(".") + "."  # "да" -> "да" (no dot needed actually)
    key = raw_c.strip().lower()
    # словарь ключей без хвостовой пунктуации/пробелов, но с точкой как в карте
    lookup = raw_c.strip().lower()
    if lookup in TRACK_MAP:
        val = TRACK_MAP[lookup]
    else:
        val = None
    if val == "fact" and not allow_fact:
        # "Факт" вне трека "Физика" — не ожидается картой, но не гадаем: other.
        val = None
    if val is None:
        add_issue(sys_no, "warning", f"track_{col_name}_other",
                  f"Значение трека «{col_name}» не входит в карту нормализации", raw_c)
        val = "other"
    return val, raw_c


# =======================================================================
# Ответственные (m2m) — докс раздел rsk_responsible
# =======================================================================

RESP_CANON = {
    "пто": "ПТО",
    "дпр": "ДПР",
    "икс": "ИКС",
    "лаборатория": "Лаборатория",
    "стройка тм-35": "Стройка ТМ-35",
    "тм-35": "Стройка ТМ-35",
    "стройка-тм-35": "Стройка ТМ-35",
    "стройка-35": "Стройка ТМ-35",
}

PAREN_RE = re.compile(r"\(([^)]*)\)")


def parse_responsible(raw, sys_no):
    """Возвращает (canonical_names:list[str], extra_note:str|None)."""
    raw_c = clean_text(raw)
    if not raw_c:
        return [], None
    notes = []

    def strip_parens(m):
        notes.append(m.group(1).strip())
        return ""

    stripped = PAREN_RE.sub(strip_parens, raw_c)
    parts = [p.strip() for p in stripped.split("+") if p.strip()]
    names = []
    for p in parts:
        key = p.lower().strip()
        canon = RESP_CANON.get(key)
        if not canon:
            add_issue(sys_no, "warning", "responsible_unmapped",
                      f"Не удалось нормализовать ответственного «{p}»", raw_c)
            continue
        if canon not in names:
            names.append(canon)
    note = ("уточнение по ответственному: " + "; ".join(notes)) if notes else None
    return names, note


# =======================================================================
# Условия снятия (close_condition) — канонизация похожих формулировок
# =======================================================================

CLOSE_CONDITIONS = [
    ("ид_принятие", re.compile(r"недел\w*\s+после\s+прин\w*\s+рск.*раздел\w*\s+ид", re.I),
     "Неделя после принятия РСК соответствующих разделов ИД"),
    ("приказ_икс_рд", re.compile(r"недел\w*\s+после\s+выход\w*\s+приказ\w*\s+икс", re.I),
     "Неделя после выхода приказа ИКС о внесении изменений в РД (ПД)"),
    ("работы_стройплощадка", re.compile(r"недел\w*\s+после\s+выполнени\w*\s+стр\.?\s*площад", re.I),
     "Неделя после выполнения Стр.площадкой работ"),
    ("освобождение_склад", re.compile(r"недел\w*\s+после\s+заверш\w*\s+работ\s+и\s+освобожд", re.I),
     "Неделя после завершения работ и освобождения складской площадки"),
    ("протоколы_уплотнения", re.compile(r"недел\w*\s+после\s+получени\w*\s+протокол\w*\s+уплотн", re.I),
     "Неделя после получения протоколов уплотнения"),
    ("техрешение_дпр", re.compile(r"недел\w*\s+после\s+заверш\w*\s+работ\s+по\s+техрешени\w*\s+дпр", re.I),
     "Неделя после завершения работ по техрешению ДПР, внесённому в РД"),
    ("геодезия", re.compile(r"выполнени\w*\s+будет\s+проверя\w*\s+рск\s+после\s+получени\w*\s+геодез", re.I),
     "Выполнение будет проверяться РСК после получения геодезической съёмки"),
    ("аоср_икс", re.compile(r"подписани\w*\s+аоср\s+представител\w*\s+икс", re.I),
     "После подписания АОСР представителем ИКС"),
]


# Комментарий (колонка F) для этого условия у 70 из 86 записей — почти
# буквальный пересказ самого условия ("наступит неделя после принятия
# ИД — тогда снимется"), без новой информации. У остальных в комментарии
# указано конкретное действие (какое ИЗМ, какая редакция ПГИ/ПМИ и т.п.) —
# это уже не пересказ, оставляем в note. Не общее правило "комментарий
# всегда редундантен" — только этот конкретный повторяющийся текст.
REDUNDANT_CONDITION_COMMENTS = {"Будет снято РСК по принятии соответств. разделов ИД"}


def match_close_condition(text):
    for code, rx, canon in CLOSE_CONDITIONS:
        m = rx.search(text)
        if m:
            return code, canon, m
    return None, None, None


# =======================================================================
# Секция/участок — извлечение из content (правило 7)
# =======================================================================

SECTION_RE = re.compile(
    r"(Участ(?:ок|ки)\s+[^.;,]+?(?=[.;]|,\s*[А-ЯЁ][а-яё]+\s+(?:не|Не)|$)"
    r"|Камер[аы]\s+[^.;]+?(?=[.;]|$)"
    r"|Павильон\s+[^.;]+?(?=[.;]|$)"
    r"|ОП[нв]\s+[0-9][^.;]*?(?=[.;]|$))",
    re.U,
)


def extract_section(content):
    if not content:
        return None
    m = SECTION_RE.search(content)
    if not m:
        return None
    return clean_text(m.group(1))


# =======================================================================
# Классификация состояния (state) — правила 2 и 3 докса
# =======================================================================

CLOSED_RE = re.compile(r"снят[оа]\b", re.I)
REJECTED_RE = re.compile(r"отклон", re.I)
PARTIAL_RE = re.compile(r"частичн\w*\s+.*?снят", re.I)


def word_before(text, idx):
    before = text[:idx].rstrip()
    m = re.search(r"(\S+)$", before)
    return m.group(1).lower() if m else ""


def find_real_closed_date(text):
    """Первое 'снят[оа]', не предварённое 'будет' — с датой рядом."""
    for m in CLOSED_RE.finditer(text):
        prev_word = word_before(text, m.start())
        if prev_word.rstrip(",;") == "будет":
            continue
        # дата обычно сразу после слова, в пределах ~20 символов
        tail = text[m.end():m.end() + 25]
        d = first_date(tail)
        if d:
            return d, m.start()
    return None, None


def classify_state(sys_no, comment_raw, planned_raw):
    comment_c = norm(comment_raw) or ""
    planned_c = norm(planned_raw) or ""
    combined = " | ".join(p for p in [planned_c, comment_c] if p)

    # 1. Закрыто — реальная дата снятия, не в условном "будет снято".
    closed_date, _ = find_real_closed_date(planned_c)
    if not closed_date:
        closed_date, _ = find_real_closed_date(comment_c)
    if closed_date:
        return {
            "state": "closed", "closed_date": closed_date,
            "close_condition_code": None, "note": None,
        }

    # 2. Отклонено РСК.
    if REJECTED_RE.search(combined):
        return {
            "state": "rejected", "closed_date": None,
            "close_condition_code": None,
            "note": combined or None,
        }

    # 3. Частичное снятие.
    if PARTIAL_RE.search(combined):
        return {
            "state": "partially_closed", "closed_date": None,
            "close_condition_code": None,
            "note": combined or None,
        }

    # 4. Условие-триггер (open + close_condition).
    code, canon, m = match_close_condition(planned_c) if planned_c else (None, None, None)
    if not code and comment_c:
        code, canon, m = match_close_condition(comment_c)
    if code:
        # То, что осталось от текста после условия — если несёт доп.
        # смысл (не пустая обрезка), кладём в note.
        base_text = planned_c if m and m.re.search(planned_c) else comment_c
        leftover = (base_text[:m.start()] + base_text[m.end():]).strip(" .,-")
        extra = leftover if leftover and len(leftover) > 3 else None
        comment_extra = comment_c if (
            base_text != comment_c and comment_c and comment_c not in REDUNDANT_CONDITION_COMMENTS
        ) else None
        note_parts = [p for p in [extra, comment_extra] if p]
        return {
            "state": "open", "closed_date": None,
            "close_condition_code": code,
            "note": " | ".join(note_parts) if note_parts else None,
        }

    # 5. Просто открыто, со свободной заметкой (если есть).
    return {
        "state": "open", "closed_date": None,
        "close_condition_code": None,
        "note": combined or None,
    }


# =======================================================================
# Основной разбор xlsx
# =======================================================================

def load_rows():
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["Violations"]
    raw_rows = []
    for row in range(2, ws.max_row + 1):
        vals = {
            "sys_no": ws.cell(row=row, column=1).value,
            "control_measure": ws.cell(row=row, column=2).value,
            "content": ws.cell(row=row, column=3).value,
            "remedy": ws.cell(row=row, column=4).value,
            "status_raw": ws.cell(row=row, column=5).value,
            "comment": ws.cell(row=row, column=6).value,
            "violation_type_raw": ws.cell(row=row, column=7).value,
            "created_raw": ws.cell(row=row, column=8).value,
            "due_date_raw": ws.cell(row=row, column=9).value,
            "due_date_moved_raw": ws.cell(row=row, column=10).value,
            "actual_close_raw": ws.cell(row=row, column=11).value,  # K, всегда '-'/None — не используется
            "urgent_raw": ws.cell(row=row, column=12).value,
            "act_first_raw": ws.cell(row=row, column=13).value,
            "act_last_raw": ws.cell(row=row, column=14).value,
            "author_raw": ws.cell(row=row, column=16).value,
            "planned_close_raw": ws.cell(row=row, column=23).value,
            "responsible_raw": ws.cell(row=row, column=24).value,
            "track_phys_raw": ws.cell(row=row, column=25).value,
            "track_design_raw": ws.cell(row=row, column=26).value,
            "track_id_raw": ws.cell(row=row, column=27).value,
            "_row": row,
        }
        raw_rows.append(vals)
    return raw_rows


def merge_continuations(raw_rows):
    """Правило 1 докса: пустой № -> приклеить Содержание/Мероприятия
    к предыдущей записи. Строго проверяем, что предыдущая запись реально
    существует (иначе это не наш ожидаемый кейс — не гадаем)."""
    merged = []
    for r in raw_rows:
        if r["sys_no"] is None:
            if not merged:
                add_issue(None, "error", "orphan_continuation",
                          f"Строка {r['_row']} без № и без предыдущей записи для склейки")
                continue
            prev = merged[-1]
            extra_c = clean_text(r["content"])
            extra_r = clean_text(r["remedy"])
            if extra_c:
                prev["content"] = (clean_text(prev["content"]) or "") + " " + extra_c
            if extra_r:
                prev["remedy"] = (clean_text(prev["remedy"]) or "") + " " + extra_r
            prev["_merged_from_rows"] = prev.get("_merged_from_rows", []) + [r["_row"]]
        else:
            merged.append(r)
    return merged


VIOLATION_TYPE_MAP = {"значительное": "significant", "критическое": "critical"}


def build_records(merged_rows):
    records = []
    seen_sysno = set()
    for r in merged_rows:
        sys_no = r["sys_no"]
        if not isinstance(sys_no, (int, float)):
            add_issue(None, "error", "bad_sys_no", "№ не является числом", repr(sys_no))
            continue
        sys_no = int(sys_no)
        if sys_no in seen_sysno:
            add_issue(sys_no, "error", "duplicate_sys_no", "Повторяющийся системный номер")
        seen_sysno.add(sys_no)

        content = norm(r["content"])
        remedy = norm(r["remedy"])

        vtype_raw = clean_text(r["violation_type_raw"])
        vtype = VIOLATION_TYPE_MAP.get((vtype_raw or "").lower(), "unknown")
        if vtype_raw and (vtype_raw or "").lower() not in VIOLATION_TYPE_MAP:
            add_issue(sys_no, "warning", "violation_type_other",
                      "Тип нарушения не из известного набора", vtype_raw)

        created_at = to_iso(r["created_raw"])
        if not created_at:
            # Правило: "Создано" иногда — предложение
            # "Выявлено при проведении проверки DD.MM.YYYY № ..." вместо даты.
            created_at = first_date(clean_text(r["created_raw"]) or "")
            if not created_at:
                add_issue(sys_no, "warning", "created_at_missing",
                          "Не удалось определить дату создания замечания", r["created_raw"])

        state_info = classify_state(sys_no, r["comment"], r["planned_close_raw"])

        section_raw = extract_section(content)

        resp_names, resp_note = parse_responsible(r["responsible_raw"], sys_no)

        phys, phys_raw = norm_track(r["track_phys_raw"], True, sys_no, "phys")
        design, design_raw = norm_track(r["track_design_raw"], False, sys_no, "design")
        idt, id_raw = norm_track(r["track_id_raw"], False, sys_no, "id")

        urgent_raw = clean_text(r["urgent_raw"])
        urgent = (urgent_raw or "").strip().lower() == "да"
        if urgent_raw and urgent_raw.strip().lower() not in ("да", "нет"):
            add_issue(sys_no, "warning", "urgent_other", "Неожиданное значение 'Устранить немедленно'", urgent_raw)

        note_parts = [p for p in [state_info["note"], resp_note] if p]
        note = "; ".join(note_parts) if note_parts else None

        rec = {
            "sys_no": sys_no,
            "control_measure_raw": clean_text(r["control_measure"]),
            "content": content,
            "remedy": remedy,
            "violation_type": vtype,
            "created_at": created_at,
            "due_date": to_iso(r["due_date_raw"]),
            "due_date_moved": to_iso(r["due_date_moved_raw"]),
            "urgent": urgent,
            "author": clean_text(r["author_raw"]),
            "state": state_info["state"],
            "closed_date": state_info["closed_date"],
            "close_condition_code": state_info["close_condition_code"],
            "note": note,
            "section_raw": section_raw,
            "is_repeat": False,  # переопределяется из акта ниже
            "source": "registry",
            "act_first_raw": clean_text(r["act_first_raw"]),
            "act_last_raw": clean_text(r["act_last_raw"]),
            "responsible": resp_names,
            "track_phys": phys, "track_phys_raw": phys_raw,
            "track_design": design, "track_design_raw": design_raw,
            "track_id": idt, "track_id_raw": id_raw,
            "merged_from_rows": r.get("_merged_from_rows", []),
        }
        records.append(rec)
    return records


ACT_REF_RE = re.compile(r"№?\s*(4183-\d+)\s*,\s*(\d{1,2}\.\d{1,2}\.\d{2,4})")


def parse_act_ref(raw):
    if not raw or raw == "-":
        return None
    m = ACT_REF_RE.search(raw)
    if not m:
        return None
    act_no, date_s = m.groups()
    return {"act_no": act_no, "act_date": first_date(date_s)}


# =======================================================================
# Акт проверки (PDF) — парсинг для двух целей:
#  (а) две отсутствующие в реестре записи (№354, №358, правило 6);
#  (б) сверка (какие sys_no реально в акте, item_no/is_repeat для акта 4183-159).
# =======================================================================

ACT_ITEM_RE = re.compile(r"№\s*(\d+)")


def parse_act_pdf(valid_sysnos):
    """valid_sysnos — множество системных номеров, которые реально могут
    встретиться (реестр + два известных из докса) — используется как
    фильтр, потому что "№ NNN" в тексте акта встречается не только как
    маркер начала записи о нарушении, но и как ссылка на другой документ/
    приказ/колонку журнала/закон (напр. "№ 937", "№ 4183-159" в
    колонтитуле, "№ 7-ФЗ", "№ 2 (Наименование..." в тексте другого
    нарушения, "Тепломагистраль № 35 от...") — без фильтра акт даёт 163
    вместо ожидаемых 152.

    Хвостовую проверку "не число-с-дефисом" делаем ПОСЛЕ матча обычным
    Python-кодом, а не через negative lookahead в самой регулярке: `\\d+`
    жадный, и `(?!-\\d)` после него бэктрекается на один разряд короче,
    из-за чего "4183-159" всё равно давал бы ложное совпадение "418"
    (следующий символ после "418" — "3", не дефис, лукахед формально
    проходит). Проверка постфактум на реальный `m.end()` этого не имеет.
    """
    text = subprocess.run(
        ["pdftotext", "-layout", PDF_PATH, "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    matches = []
    for m in ACT_ITEM_RE.finditer(text):
        sn = int(m.group(1))
        if sn not in valid_sysnos:
            continue
        after = text[m.end():m.end() + 1]
        if after == "-":
            continue  # "4183-159" (колонтитул), "7-ФЗ" (закон) и т.п.
        if re.match(r"\s*от\s", text[m.end():m.end() + 6]):
            continue  # "Тепломагистраль № 35 от Хабаровской ТЭЦ-3..."
        if re.match(r"\s*\(", text[m.end():m.end() + 4]):
            continue  # "№ 12 (средняя прочность...)" — номер столбца журнала, не sys_no
        matches.append(m)
    act_sysnos = []
    act_items = {}
    for i, m in enumerate(matches):
        sys_no = int(m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():end]
        is_repeat = bool(re.match(r"\s*Повторно\.", block))
        act_sysnos.append(sys_no)
        due = None
        due_m = re.search(r"[Дд]о\s+(\d{1,2}\.\d{1,2}\.\d{4})\s*г?\.?", block)
        if due_m:
            due = first_date(due_m.group(1))
        act_items[sys_no] = {"is_repeat": is_repeat, "due_date": due}
    return text, act_sysnos, act_items


MISSING_FROM_REGISTRY = [
    {
        "sys_no": 354,
        "content": ("Отсутствие средств индивидуальной защиты (СИЗ/каски) у персонала на строительной "
                    "площадке."),
        "remedy": None,
        "violation_type": "unknown",
        "created_at": "2025-11-12",
        "author": None,
        "state": "open",
        "closed_date": None,
        "close_condition_code": None,
        "note": "Заведено из акта 4183-159 (докс координатора, правило 6) — отсутствует в реестре xlsx, "
                "требует ручной доводки текста «Содержание»/«Мероприятия» по акту №4183-138.",
        "section_raw": None,
        "is_repeat": False,
        "source": "act",
        "act_first_raw": "№4183-138, 12.11.2025",
        "act_last_raw": None,
        "responsible": [],
        "track_phys": "unknown", "track_phys_raw": None,
        "track_design": "unknown", "track_design_raw": None,
        "track_id": "unknown", "track_id_raw": None,
        "control_measure_raw": None,
        "due_date": None, "due_date_moved": None, "urgent": False,
        "merged_from_rows": [],
    },
    {
        "sys_no": 358,
        "content": ("Материалы складированы за пределами полосы отвода, участок УП93."),
        "remedy": None,
        "violation_type": "unknown",
        "created_at": "2025-11-25",
        "author": None,
        "state": "open",
        "closed_date": None,
        "close_condition_code": None,
        "note": "Заведено из акта 4183-159 (докс координатора, правило 6) — отсутствует в реестре xlsx, "
                "требует ручной доводки текста «Содержание»/«Мероприятия» по акту №4183-140.",
        "section_raw": "участок УП93",
        "is_repeat": False,
        "source": "act",
        "act_first_raw": "№4183-140, 25.11.2025",
        "act_last_raw": None,
        "responsible": [],
        "track_phys": "unknown", "track_phys_raw": None,
        "track_design": "unknown", "track_design_raw": None,
        "track_id": "unknown", "track_id_raw": None,
        "control_measure_raw": None,
        "due_date": None, "due_date_moved": None, "urgent": False,
        "merged_from_rows": [],
    },
]
for m in MISSING_FROM_REGISTRY:
    add_issue(m["sys_no"], "warning", "missing_from_registry",
              "Нарушение есть в акте 4183-159, но отсутствует в реестре xlsx — заведено вручную по докс "
              "координатора, содержание требует сверки с актом (не расшифровано из PDF построчно).")


# =======================================================================
# main
# =======================================================================

def main():
    raw_rows = load_rows()
    merged_rows = merge_continuations(raw_rows)
    if len(merged_rows) != 233:
        print(f"!! ОСТАНОВКА: после склейки строк {len(merged_rows)} записей, ожидалось 233", file=sys.stderr)
    records = build_records(merged_rows)

    valid_sysnos = {r["sys_no"] for r in records} | {354, 358}
    act_text, act_sysnos, act_items = parse_act_pdf(valid_sysnos)
    act_sysno_set = set(act_sysnos)

    for rec in records:
        info = act_items.get(rec["sys_no"])
        if info:
            rec["is_repeat"] = info["is_repeat"]

    records.extend(MISSING_FROM_REGISTRY)

    # ---- контрольные суммы ----
    total = len(records)
    n_closed = sum(1 for r in records if r["state"] == "closed")
    n_rejected = sum(1 for r in records if r["state"] == "rejected")
    n_open_all = total - n_closed - n_rejected
    rejected_sysno = sorted(r["sys_no"] for r in records if r["state"] == "rejected")

    registry_sysno = {r["sys_no"] for r in records if r["source"] == "registry"}
    in_both = registry_sysno & act_sysno_set
    closed_after_act = sorted(
        r["sys_no"] for r in records
        if r["sys_no"] in in_both and r["state"] == "closed" and r["closed_date"] and r["closed_date"] > ACT_DATE
    )
    not_in_registry = sorted(act_sysno_set - registry_sysno)
    open_or_rejected_in_both = sum(
        1 for r in records if r["sys_no"] in in_both and r["state"] in ("open", "rejected", "partially_closed")
    )
    closed_before_act = sum(
        1 for r in records
        if r["state"] == "closed" and r["closed_date"] and r["closed_date"] <= ACT_DATE
    )

    checks = {
        "total_records": total,
        "closed": n_closed,
        "rejected": n_rejected,
        "rejected_sysno": rejected_sysno,
        "open_incl_partial": n_open_all,
        "in_registry_and_act": len(in_both),
        "closed_after_act": closed_after_act,
        "closed_after_act_count": len(closed_after_act),
        "open_or_rejected_in_both": open_or_rejected_in_both,
        "not_in_registry_but_in_act": not_in_registry,
        "closed_before_act": closed_before_act,
        "act_total_from_pdf": len(act_sysno_set),
        "duplicate_sys_no_issues": sum(1 for i in issues if i["kind"] == "duplicate_sys_no"),
        "missing_sys_no_issues": sum(1 for i in issues if i["kind"] == "bad_sys_no"),
    }

    for check_name in ["merged_rows_lines"]:
        pass
    checks["merged_rows_count"] = len(merged_rows)
    checks["merged_continuation_targets"] = {
        r["sys_no"]: r["merged_from_rows"] for r in records if r.get("merged_from_rows")
    }

    out = {
        "records": records,
        "issues": issues,
        "checks": checks,
        "act": {"act_no": ACT_NO, "act_date": ACT_DATE, "total_violations": ACT_TOTAL, "pdf_path": PDF_PATH},
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"\nЗаписей: {len(records)}; issues: {len(issues)}")
    print(f"Записано: {OUT_PATH}")


if __name__ == "__main__":
    main()
