"""
Заход 4, 10.09.2026, задача 1 — вынесено из tools/import_grafik_id_groups.py
и tools/match_groups_v3.py: до этой правки один и тот же алгоритм
разбора кода раздела ("ОПн1", "Н1-4", "КД-1") и поиска кандидата
id_form_row по префиксу+номеру существовал в коде ДВАЖДЫ, почти
дословно — тот самый класс болезни (два места считают одно и то же и
рискуют разойтись), который ищет аудит 08.09.2026, только в алгоритме
сопоставления, а не в SQL-агрегате. Третья задача, которой понадобилась
та же логика (экран решения category→tabs, main.py), стала поводом
свести все три места к одному тексту — не писать третью копию.

Модуль не знает о БД напрямую (`query_fn` передаётся вызывающим кодом) —
чтобы им одинаково пользовались и standalone-скрипты tools/*.py (через
db.query), и main.py (через свой query()).
"""
import math
import re

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
    "КД-1"). Возвращает (prefix_lower, число) или None."""
    m = LABEL_RE.match(label.strip())
    if not m:
        return None
    prefix, num = m.groups()
    return prefix.lower(), to_float(num)


def tokenize_group_name(name):
    """Название раздела/группы -> список текстовых кодов-кандидатов.
    Разделители — запятая, точка с запятой, "+" (довесок "19-47 +57")."""
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
    переносом префикса на беспрефиксные токены внутри одной группы."""
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


# Подсказка "категория -> вкладка" без явного решения человека (v3, ночной
# прогон): вкладка считается подсказкой, если её название целиком входит
# в текст категории. Используется ТОЛЬКО когда allowed_tabs не передан
# (старое поведение двух существующих скриптов, не трогаем). Новый экран
# (main.py, заход 4) явные решения человека кладёт в allowed_tabs и эту
# эвристику не использует вовсе.
def category_hint_tab_ids(idx, category):
    cat_lower = (category or "").lower()
    hints = set()
    for tab_id, label in idx["tab_labels"].items():
        if label.lower() in cat_lower:
            hints.add(tab_id)
    return hints


def build_row_index(query_fn):
    rows = query_fn("select id, tab_id, section_label, construction_label from id_form_row")
    tabs = query_fn("select id, code, label from id_form_tab")
    tab_labels = {t["id"]: t["label"] for t in tabs}
    tab_codes = {t["id"]: t["code"] for t in tabs}

    by_prefix = {}
    prefix_tabs = {}
    by_literal = {}
    by_literal_stripped = {}

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
        "by_prefix": by_prefix, "prefix_tabs": prefix_tabs,
        "by_literal": by_literal, "by_literal_stripped": by_literal_stripped,
        "tab_labels": tab_labels, "tab_codes": tab_codes,
    }


def prefix_candidates(idx, prefix, category=None, allowed_tabs=None):
    """Кандидаты (число, row_id) по префиксу.

    `allowed_tabs` (новое, заход 4) — явный, отданный человеком набор
    id вкладок для категории этой группы: кандидаты сужаются РОВНО до
    него, эвристика `category_hint_tab_ids` не участвует вовсе (человек
    уже решил). Если после сужения кандидатов нет — префикс не
    встречается ни в одной из выбранных вкладок, это не то же самое,
    что "коллизия" старого пути.

    Без `allowed_tabs` — прежнее поведение двух исходных скриптов:
    однозначный префикс проходит сразу, коллизия — через эвристику
    `category_hint_tab_ids`, иначе явный отказ с перечислением вкладок."""
    tabs = idx["prefix_tabs"].get(prefix)
    if not tabs:
        return None, "нет ни одного раздела с таким префиксом в базе"

    if allowed_tabs is not None:
        narrowed = tabs & allowed_tabs
        if not narrowed:
            return None, "префикс не встречается ни в одной из выбранных для категории вкладок"
        return [(n, rid) for (n, rid, t) in idx["by_prefix"][prefix] if t in narrowed], None

    if len(tabs) == 1:
        return [(n, rid) for (n, rid, _t) in idx["by_prefix"][prefix]], None
    hints = category_hint_tab_ids(idx, category)
    hinted = tabs & hints
    if len(hinted) == 1:
        cands = [(n, rid) for (n, rid, t) in idx["by_prefix"][prefix] if t in hinted]
        if cands:
            return cands, None
    tab_names = ", ".join(sorted(idx["tab_labels"].get(t, str(t)) for t in tabs))
    return None, f"префикс «{prefix}» встречается в нескольких вкладках ({tab_names}) — не сужаю вслепую"


def resolve_range(idx, prefix, lo, hi, category=None, allowed_tabs=None):
    """Кандидаты диапазона — по «основной части номера» (floor): «Н1-4»
    включает Н2.1 (floor=2, попадает в [1,4])."""
    if prefix not in idx["by_prefix"]:
        return None, "нет ни одного раздела с таким префиксом в базе"
    cands, err = prefix_candidates(idx, prefix, category, allowed_tabs)
    if err:
        return None, err
    lo_i, hi_i = math.floor(lo), math.floor(hi)
    matched = [row_id for (num, row_id) in cands if lo_i <= math.floor(num) <= hi_i]
    if not matched:
        return None, f"диапазон {lo_i}-{hi_i} по префиксу «{prefix}» не нашёл ни одного раздела"
    return matched, None


def resolve_single(idx, prefix, num, category=None, allowed_tabs=None):
    """Точное равенство, не floor(): одиночный код означает именно этот
    номер, не "любой номер с тем же целым основанием" (иначе "КД-1"
    ловил бы заодно КД1.1/КД1.2 как ложных кандидатов)."""
    if prefix not in idx["by_prefix"]:
        return None, "нет ни одного раздела с таким префиксом в базе"
    cands, err = prefix_candidates(idx, prefix, category, allowed_tabs)
    if err:
        return None, err
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


def resolve_group_tokens(idx, group_label, category=None, allowed_tabs=None):
    """Один разбор группы целиком -> (member_row_ids: set, reasons: list[str]).
    Общая точка, которой пользуются и импорт, и обе версии доукомплектования."""
    tokens = resolve_tokens(tokenize_group_name(group_label))
    member_row_ids = set()
    reasons = []
    for tok in tokens:
        if tok["kind"] == "range":
            cands, err = resolve_range(idx, tok["prefix"], tok["lo"], tok["hi"], category, allowed_tabs)
        elif tok["kind"] == "single":
            cands, err = resolve_single(idx, tok["prefix"], tok["num"], category, allowed_tabs)
        else:
            cands, err = resolve_literal(idx, tok["text"])

        if err:
            reasons.append(f"«{tok.get('text', '')}»: {err}")
            continue
        for rid in cands:
            member_row_ids.add(rid)
    return member_row_ids, reasons
