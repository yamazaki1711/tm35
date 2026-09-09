#!/usr/bin/env python3
"""
Импорт v2 «График ИД» — группы вместо одной строки-раздела (часть 3,
09.09.2026). Заменяет tools/import_grafik_id_report_meta.py: та схема
(id_row_report_meta, row_id unique) не могла принять группу из
нескольких id_form_row на одну строку Excel ("Н1-4" = 5 строк БД) —
отсюда 17 из 133 в части 1, не 13% реальных расхождений данных.

Модель: id_report_group — создаётся ВСЕГДА, для всех 133 строк CSV
(метаданные участок/категория/тип/исполнитель/КС-2 не зависят от
сопоставления с id_form_row). Участники (id_report_group_row) —
отдельный, второй проход: разбор текста name на коды/диапазоны,
поиск кандидатов id_form_row по префиксу+номеру.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/import_grafik_id_groups.py
"""
import csv
import math
import re
import sys

sys.path.insert(0, "/app")
from db import query, run_in_transaction  # noqa: E402

CSV_PATH = "/app/docs_import/grafik_id_extract_20260901.csv"
UNMATCHED_PATH = "/app/tools/import_grafik_id_unmatched_v2.csv"

LABEL_RE = re.compile(r"^([А-Яа-яЁё]+)\s*[-–]?\s*(\d+(?:[.,]\d+)?)$")
RANGE_RE = re.compile(
    r"^([А-Яа-яЁё]*)\s*[-–]?\s*(\d+(?:[.,]\d+)?)\s*[-–]\s*([А-Яа-яЁё]*)\s*(\d+(?:[.,]\d+)?)$"
)
PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def norm_ws(s):
    if not s:
        return ""
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_literal(s):
    return norm_ws(s).lower()


def strip_paren(s):
    return PAREN_RE.sub("", s).strip()


def to_float(s):
    return float(s.replace(",", "."))


def parse_label(label):
    """prefix+число из одиночного, неразбитого текста ("ОПн1", "ОПВ 1",
    "КД-1") — используется для индексации id_form_row по префиксу+номеру.
    Возвращает (prefix_lower, число) или None, если строка сложнее
    (сегментное имя вида "Н8-Н19", "УТ2.2-КР1" и т.п. — не подходит под
    этот индекс, останется доступной только для литерального сравнения)."""
    m = LABEL_RE.match(label.strip())
    if not m:
        return None
    prefix, num = m.groups()
    return prefix.lower(), to_float(num)


def tokenize_group_name(name):
    """Название раздела/группы из CSV -> список текстовых кодов-кандидатов.
    Разделители — запятая, точка с запятой, "+" (довесок вида "19-47 +57");
    переносы строк внутри многострочных названий — не разделитель по
    смыслу (перенос вёрстки Excel), схлопываются в пробел до разбиения."""
    s = norm_ws(name)
    parts = re.split(r"[;,]", s)
    tokens = []
    for part in parts:
        for sub in part.split("+"):
            sub = sub.strip()
            if sub:
                tokens.append(sub)
    return tokens


def resolve_tokens(tokens):
    """Каждый токен -> {'kind': 'range'|'single'|'literal', ...}, с
    переносом префикса на беспрефиксные токены внутри одной группы
    ("ОПн24, 58, 111.1-114.1, 116" — 58 и диапазон наследуют «ОПн»)."""
    active_prefix = None
    resolved = []
    for t in tokens:
        m = RANGE_RE.match(t)
        if m:
            p1, n1, p2, n2 = m.groups()
            if p2:
                resolved.append({"kind": "literal", "text": t})
                continue
            prefix = (p1 or active_prefix)
            if prefix is None:
                resolved.append({"kind": "literal", "text": t})
                continue
            active_prefix = prefix
            resolved.append({"kind": "range", "prefix": prefix.lower(),
                              "lo": to_float(n1), "hi": to_float(n2), "text": t})
            continue
        m2 = LABEL_RE.match(t)
        if m2:
            prefix, num = m2.groups()
            active_prefix = prefix
            resolved.append({"kind": "single", "prefix": prefix.lower(), "num": to_float(num), "text": t})
            continue
        m3 = re.match(r"^(\d+(?:[.,]\d+)?)$", t)
        if m3 and active_prefix:
            resolved.append({"kind": "single", "prefix": active_prefix.lower(), "num": to_float(m3.group(1)), "text": t})
            continue
        resolved.append({"kind": "literal", "text": t})
    return resolved


# Единственная подсказка "категория CSV -> вкладка", которую решился
# применить: прямое текстовое совпадение названия категории с названием
# вкладки ("Обвязка камер" -> вкладка "Обвязка"), не эвристика. Пробовал
# также связать тип "фм"/"рсм" с парой вкладок "1. ОПН"/"2. ОПН (рсм)" —
# проверка показала, что это НЕ работает: обе вкладки содержат вперемешку
# и ОПн-, и ОПв-, и Н-префиксы (см. decisions_needed, часть 3, п.17),
# тип — свойство конструктивного элемента (способ фундамента), не
# признак вкладки. Эту гипотезу не применял.
CATEGORY_TAB_HINTS = {"обвязка": "obvyazka"}


def build_row_index():
    rows = query("select id, tab_id, section_label, construction_label from id_form_row")
    tab_labels = {r["id"]: r["label"] for r in query("select id, label from id_form_tab")}
    tab_codes = {r["id"]: r["code"] for r in query("select id, code from id_form_tab")}

    by_prefix = {}       # prefix_lower -> list of (number, row_id, tab_id)
    prefix_tabs = {}      # prefix_lower -> set(tab_id)
    by_literal = {}       # normalized full label -> set(row_id)
    by_literal_stripped = {}  # normalized, paren-stripped -> set(row_id)

    for r in rows:
        for field in ("construction_label", "section_label"):
            label = r[field]
            if not label:
                continue
            parsed = parse_label(label)
            if parsed:
                prefix, num = parsed
                by_prefix.setdefault(prefix, []).append((num, r["id"], r["tab_id"]))
                prefix_tabs.setdefault(prefix, set()).add(r["tab_id"])
            lit = norm_literal(label)
            by_literal.setdefault(lit, set()).add(r["id"])
            stripped = norm_literal(strip_paren(label))
            if stripped != lit:
                by_literal_stripped.setdefault(stripped, set()).add(r["id"])

    return {
        "by_prefix": by_prefix,
        "prefix_tabs": prefix_tabs,
        "by_literal": by_literal,
        "by_literal_stripped": by_literal_stripped,
        "tab_labels": tab_labels,
        "tab_codes": tab_codes,
    }


def _prefix_candidates(idx, prefix, category):
    """Список (число, row_id) для префикса — все вкладки, если префикс
    однозначен; если коллизия и категория даёт прямую текстовую
    подсказку (CATEGORY_TAB_HINTS) — сузить до неё; иначе — коллизия,
    вернуть None с текстом причины."""
    tabs = idx["prefix_tabs"][prefix]
    if len(tabs) == 1:
        return [(n, rid) for (n, rid, _t) in idx["by_prefix"][prefix]], None
    hint_tab_code = None
    cat_lower = (category or "").lower()
    for keyword, tab_code in CATEGORY_TAB_HINTS.items():
        if keyword in cat_lower:
            hint_tab_code = tab_code
            break
    if hint_tab_code:
        hinted_tab_ids = {tid for tid in tabs if idx["tab_codes"].get(tid) == hint_tab_code}
        if hinted_tab_ids:
            cands = [(n, rid) for (n, rid, t) in idx["by_prefix"][prefix] if t in hinted_tab_ids]
            if cands:
                return cands, None
    tab_names = ", ".join(sorted(idx["tab_labels"].get(t, str(t)) for t in tabs))
    return None, f"префикс «{prefix}» встречается в нескольких вкладках ({tab_names}) — не сужаю вслепую"


def resolve_range(idx, prefix, lo, hi, category=None):
    """Кандидаты диапазона — по «основной части номера» (floor), не по
    сырому числу: «Н1-4» должен включать Н2.1 (floor=2, попадает в
    [1,4]), а не только целые 1..4 (координатор, часть 3)."""
    if prefix not in idx["by_prefix"]:
        return None, "нет ни одного раздела с таким префиксом в базе"
    cands, err = _prefix_candidates(idx, prefix, category)
    if err:
        return None, err
    lo_i, hi_i = math.floor(lo), math.floor(hi)
    matched = [row_id for (num, row_id) in cands if lo_i <= math.floor(num) <= hi_i]
    if not matched:
        return None, f"диапазон {lo_i}-{hi_i} по префиксу «{prefix}» не нашёл ни одного раздела"
    return matched, None


def resolve_single(idx, prefix, num, category=None):
    if prefix not in idx["by_prefix"]:
        return None, "нет ни одного раздела с таким префиксом в базе"
    cands, err = _prefix_candidates(idx, prefix, category)
    if err:
        return None, err
    # Точное равенство, не floor(): одиночный код ("58", "116") означает
    # именно этот номер, а не "любой номер с тем же целым основанием" —
    # иначе "КД-1" ловил бы заодно КД1.1/КД1.2 как ложных кандидатов.
    # Округление по основной части — только для диапазонов (resolve_range),
    # так и просила задача.
    matched = [row_id for (n, row_id) in cands if n == num]
    if not matched:
        return None, f"номер {num} по префиксу «{prefix}» не нашёлся"
    if len(matched) > 1:
        return None, f"номер {num} по префиксу «{prefix}» — несколько кандидатов: {matched}"
    return matched, None


def resolve_literal(idx, text):
    key = norm_literal(text)
    cands = idx["by_literal"].get(key)
    if cands and len(cands) == 1:
        return list(cands), None
    if cands and len(cands) > 1:
        return None, f"текст «{text}» совпал буквально с несколькими разделами: {sorted(cands)}"
    stripped_key = norm_literal(strip_paren(text))
    cands2 = idx["by_literal_stripped"].get(stripped_key)
    if cands2 and len(cands2) == 1:
        return list(cands2), None
    if cands2 and len(cands2) > 1:
        return None, f"текст «{text}» (без скобочного уточнения) совпал с несколькими разделами: {sorted(cands2)}"
    return None, f"текст «{text}» не найден ни буквально, ни без скобочного уточнения"


def main():
    csv_rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    print(f"прочитано строк CSV: {len(csv_rows)}")

    idx = build_row_index()

    uchastok_order = []
    display_order_counter = {}
    groups = []  # каждая запись: dict с метаданными + resolved member row_ids + unmatched notes

    claimed_by = {}  # row_id -> group_source_row (для конфликтов "уже занят другой группой")
    unmatched_log = []

    for csv_row in csv_rows:
        uch_label = csv_row["uchastok"].strip()
        if uch_label not in uchastok_order:
            uchastok_order.append(uch_label)
        uchastok_no = uchastok_order.index(uch_label) + 1
        category = csv_row["category"].strip()
        key = (uchastok_no, category)
        display_order_counter[key] = display_order_counter.get(key, 0) + 1
        display_order = display_order_counter[key]

        cost_raw = (csv_row.get("cost_mln") or "").strip()
        cost_mln = float(cost_raw) if cost_raw else None
        source_row = int(csv_row["row"])
        group_label = csv_row["name"]

        tokens = resolve_tokens(tokenize_group_name(group_label))
        member_row_ids = set()
        for tok in tokens:
            if tok["kind"] == "range":
                cands, err = resolve_range(idx, tok["prefix"], tok["lo"], tok["hi"], category)
            elif tok["kind"] == "single":
                cands, err = resolve_single(idx, tok["prefix"], tok["num"], category)
            else:
                cands, err = resolve_literal(idx, tok["text"])

            if err:
                unmatched_log.append({
                    "source_row": source_row, "group_label": group_label,
                    "token": tok.get("text", f"{tok.get('prefix','')}{tok.get('num', tok.get('lo'))}"),
                    "kind": tok["kind"], "reason": err,
                })
                continue

            for rid in cands:
                if rid in claimed_by and claimed_by[rid] != source_row:
                    unmatched_log.append({
                        "source_row": source_row, "group_label": group_label,
                        "token": tok.get("text", ""), "kind": tok["kind"],
                        "reason": f"раздел id={rid} уже занят группой из строки CSV {claimed_by[rid]}",
                    })
                    continue
                claimed_by[rid] = source_row
                member_row_ids.add(rid)

        groups.append({
            "uchastok_no": uchastok_no, "uchastok_label": uch_label,
            "category_group": category,
            "group_label": group_label,
            "type_label": (csv_row.get("type") or "").strip() or None,
            "executor_name": (csv_row.get("executor") or "").strip() or None,
            "display_order": display_order,
            "ks2_cost_mln": cost_mln,
            "source_row": source_row,
            "member_row_ids": sorted(member_row_ids),
        })

    def _do(cur):
        for g in groups:
            cur.execute(
                """insert into id_report_group
                   (uchastok_no, uchastok_label, category_group, group_label, type_label,
                    executor_name, display_order, ks2_cost_mln, source_row)
                   values (%(uchastok_no)s, %(uchastok_label)s, %(category_group)s, %(group_label)s,
                           %(type_label)s, %(executor_name)s, %(display_order)s, %(ks2_cost_mln)s, %(source_row)s)
                   returning id""",
                g,
            )
            group_id = cur.fetchone()["id"]
            for rid in g["member_row_ids"]:
                cur.execute(
                    "insert into id_report_group_row (group_id, row_id) values (%s, %s)",
                    (group_id, rid),
                )

    run_in_transaction(_do)

    if unmatched_log:
        with open(UNMATCHED_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["source_row", "group_label", "token", "kind", "reason"])
            w.writeheader()
            for u in unmatched_log:
                w.writerow(u)

    total_groups = len(groups)
    groups_with_members = sum(1 for g in groups if g["member_row_ids"])
    groups_empty = total_groups - groups_with_members
    total_members = sum(len(g["member_row_ids"]) for g in groups)

    print(f"групп создано: {total_groups} (всегда все строки CSV)")
    print(f"групп хотя бы с одним разделом: {groups_with_members}")
    print(f"групп без единого раздела: {groups_empty}")
    print(f"всего привязок раздел->группа: {total_members}")
    print(f"строк в unmatched_v2 (отдельные коды/конфликты): {len(unmatched_log)} -> {UNMATCHED_PATH}")


if __name__ == "__main__":
    main()
