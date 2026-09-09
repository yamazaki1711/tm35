#!/usr/bin/env python3
"""
Ночной прогон 09-10.09.2026, задача 5 — вторая попытка сопоставления
для групп, оставшихся БЕЗ единого раздела после части 3 (81 из 133 на
момент запуска). Не трогает уже заполненные группы, не создаёт новых
групп (это делает tools/import_grafik_id_groups.py) — только пробует
доукомплектовать пустые.

Что нового по сравнению с частью 3 (см. decisions_needed п.18):
подсказка "категория CSV -> вкладка" расширена с одного проверенного
случая ("Обвязка камер" -> "Обвязка") до общего правила: если название
вкладки (например "Камеры") целиком входит текстом в категорию группы
(например "Камеры" или "Обвязка камер") — и это единственная вкладка
среди коллизии, для которой это верно, — используем её. Для "Камеры и
колодцы" правило НЕ срабатывает (совпадает срузу с "Камеры" И
"Колодцы" — остаётся неоднозначным, не гадаем).

Пишет ОТЧЁТ (docs/match_groups_v3_report.md), не тихую запись в БД:
сколько было/стало, что удалось разрешить и почему, что осталось
нерешённым и почему. Запись в id_report_group_row — только для
однозначных случаев.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/match_groups_v3.py
"""
import math
import re
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
from db import query, query_one, run_in_transaction  # noqa: E402

REPORT_PATH = "/app/docs_report/match_groups_v3_report.md"

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
    m = LABEL_RE.match(label.strip())
    if not m:
        return None
    prefix, num = m.groups()
    return prefix.lower(), to_float(num)


def tokenize_group_name(name):
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


def build_row_index():
    rows = query("select id, tab_id, section_label, construction_label from id_form_row")
    tabs = query("select id, code, label from id_form_tab")
    tab_labels = {t["id"]: t["label"] for t in tabs}
    tab_codes = {t["id"]: t["code"] for t in tabs}
    all_tab_ids = list(tab_labels.keys())

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
        "tab_labels": tab_labels, "tab_codes": tab_codes, "all_tab_ids": all_tab_ids,
    }


def category_hint_tab_ids(idx, category):
    """Новое в v3: общее правило вместо одного хардкода "Обвязка камер".
    Вкладка считается подсказкой категории, если её название целиком
    входит в текст категории (без учёта регистра). "Камеры и колодцы"
    задевает сразу "Камеры" И "Колодцы" — намеренно НЕ сужаем (см.
    docstring модуля)."""
    cat_lower = (category or "").lower()
    hints = set()
    for tab_id, label in idx["tab_labels"].items():
        if label.lower() in cat_lower:
            hints.add(tab_id)
    return hints


def _prefix_candidates(idx, prefix, category):
    tabs = idx["prefix_tabs"].get(prefix)
    if not tabs:
        return None, "нет ни одного раздела с таким префиксом в базе"
    if len(tabs) == 1:
        return [(n, rid) for (n, rid, _t) in idx["by_prefix"][prefix]], None
    hints = category_hint_tab_ids(idx, category)
    narrowed = tabs & hints
    if len(narrowed) == 1:
        cands = [(n, rid) for (n, rid, t) in idx["by_prefix"][prefix] if t in narrowed]
        if cands:
            return cands, None
    tab_names = ", ".join(sorted(idx["tab_labels"].get(t, str(t)) for t in tabs))
    return None, f"префикс «{prefix}» встречается в нескольких вкладках ({tab_names}) — не сужаю вслепую"


def resolve_range(idx, prefix, lo, hi, category=None):
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
    empty_groups = query("""
        select g.id, g.source_row, g.group_label, g.category_group
        from id_report_group g
        left join id_report_group_row gr on gr.group_id = g.id
        where gr.group_id is null
        order by g.source_row
    """)
    print(f"групп без единого раздела на начало: {len(empty_groups)}")

    already_claimed = {r["row_id"] for r in query("select row_id from id_report_group_row")}
    idx = build_row_index()

    resolved = []
    still_unresolved = []

    for g in empty_groups:
        tokens = resolve_tokens(tokenize_group_name(g["group_label"]))
        member_row_ids = set()
        reasons = []
        for tok in tokens:
            if tok["kind"] == "range":
                cands, err = resolve_range(idx, tok["prefix"], tok["lo"], tok["hi"], g["category_group"])
            elif tok["kind"] == "single":
                cands, err = resolve_single(idx, tok["prefix"], tok["num"], g["category_group"])
            else:
                cands, err = resolve_literal(idx, tok["text"])

            if err:
                reasons.append(f"«{tok.get('text','')}»: {err}")
                continue
            for rid in cands:
                if rid in already_claimed:
                    reasons.append(f"«{tok.get('text','')}»: раздел id={rid} уже занят другой группой")
                    continue
                member_row_ids.add(rid)

        if member_row_ids:
            resolved.append({"group": g, "row_ids": sorted(member_row_ids), "partial_reasons": reasons})
        else:
            still_unresolved.append({"group": g, "reasons": reasons})

    def _do(cur):
        for r in resolved:
            for rid in r["row_ids"]:
                cur.execute(
                    "insert into id_report_group_row (group_id, row_id) values (%s, %s) on conflict do nothing",
                    (r["group"]["id"], rid),
                )
                already_claimed.add(rid)

    if resolved:
        run_in_transaction(_do)

    total_after = query_one("""
        select count(*) as n from id_report_group g
        where exists (select 1 from id_report_group_row gr where gr.group_id = g.id)
    """)["n"]

    lines = []
    lines.append("# Отчёт второй попытки сопоставления групп (v3) — ночной прогон, задача 5")
    lines.append("")
    lines.append(f"Групп без единого раздела на старте: **{len(empty_groups)}**.")
    lines.append(f"Из них доукомплектовано в этом проходе: **{len(resolved)}**.")
    lines.append(f"Осталось без единого раздела: **{len(still_unresolved)}**.")
    lines.append(f"Всего групп хотя бы с одним разделом теперь: **{total_after}** из 133.")
    lines.append("")
    lines.append("Новое в v3 по сравнению с частью 3: подсказка «категория → вкладка» "
                  "обобщена с единственного проверенного случая («Обвязка камер» → «Обвязка») "
                  "до общего правила — название вкладки, целиком входящее в текст категории, "
                  "берётся как подсказка, если это единственная такая вкладка среди коллизии. "
                  "«Камеры и колодцы» под это не подпадает (задевает сразу «Камеры» и «Колодцы») — "
                  "остаётся неоднозначным намеренно, не гадаем.")
    lines.append("")
    lines.append("## Доукомплектованные группы")
    lines.append("")
    if resolved:
        lines.append("| Строка | Группа | Категория | Добавлено разделов |")
        lines.append("|---|---|---|---|")
        for r in resolved:
            g = r["group"]
            lines.append(f"| {g['source_row']} | {g['group_label']} | {g['category_group']} | {len(r['row_ids'])} |")
    else:
        lines.append("(ни одной)")
    lines.append("")
    lines.append("## Осталось без единого раздела — с причиной")
    lines.append("")
    lines.append("| Строка | Группа | Категория | Причины |")
    lines.append("|---|---|---|---|")
    for u in still_unresolved:
        g = u["group"]
        reasons_text = "; ".join(u["reasons"][:5]) if u["reasons"] else "(токены не разобрались вовсе)"
        lines.append(f"| {g['source_row']} | {g['group_label']} | {g['category_group']} | {reasons_text} |")

    import os
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"доукомплектовано групп: {len(resolved)}")
    print(f"осталось без раздела: {len(still_unresolved)}")
    print(f"всего с разделами теперь: {total_after} из 133")
    print(f"отчёт: {REPORT_PATH}")


if __name__ == "__main__":
    main()
