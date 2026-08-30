"""
Проверка главного вопроса координатора: можно ли реально нормировать
виды работ по ТМ-35? Тест — сопоставить 512 позиций ведомости работ по
РД (смета, реальные физические объёмы м3/м2/шт — в отличие от 163 работ
Excel ПТО, где объёмов нет) со справочником норм: сначала СТО-ССР
(основной), при отсутствии — ГЭСН (вспомогательный).

Используются все поправки, найденные сегодня при сверке СТО-ССР/ГЭСН:
приведение единицы ГЭСН к базису 1 (100/1000 -> 1), явная проверка
конфликта материала (сталь vs ПЭ и т.п.), проверка полярности
монтаж/демонтаж, требование совпадения диаметра/числового параметра,
когда он есть в названии — без этого сопоставление даёт мусор (см.
docs/ssr_vs_gesn_crosscheck_2026-08-19.md).
"""
import json
import re

STOPWORDS = {
    "и", "в", "с", "по", "для", "на", "от", "до", "из", "к", "не", "или",
    "при", "без", "под", "за", "работы", "работ", "устройство", "монтаж",
    "установка", "прочие", "конструкции", "конструкций", "трубы",
    "трубопроводов", "труб", "выполнение", "производство",
}
MATERIAL_GROUPS = [
    {"стальной", "стальная", "стальные", "сталь", "стали", "стальных"},
    {"полиэтиленовых", "полиэтиленовой", "пэ", "пвх", "пластиковых", "пластикового"},
    {"чугунных", "чугунный", "чугуна"},
    {"железобетонных", "жб", "бетонных", "бетона"},
]
NEGATIVE_WORDS = {"демонтаж", "разборка", "снятие", "удаление", "снос", "выемка"}
POSITIVE_WORDS = {"монтаж", "устройство", "установка", "укладка", "прокладка",
                   "сборка", "возведение", "строительство", "засыпка", "сварка"}
UNIT_ALIASES = {
    "м3": {"м3", "м 3", "куб.м"}, "м2": {"м2", "м 2", "кв.м"},
    "м": {"м", "мп", "пог.м", "п.м", "п.м."}, "т": {"т", "тн"}, "шт": {"шт", "шт."},
    "стык": {"стык"}, "рез": {"рез"}, "компл": {"компл", "комп", "комп.", "комплект"},
    "конс": {"конс", "констр", "конструкция"},
}
UNIT_MULT_RE = re.compile(r"^(\d[\d\s]*)\s*(.+)$")


def norm_unit_and_mult(u):
    if not u:
        return None, 1.0
    u = u.strip().lower().rstrip(".")
    m = UNIT_MULT_RE.match(u)
    mult = 1.0
    if m:
        mult = float(m.group(1).replace(" ", ""))
        u = m.group(2).strip()
    for canon, aliases in UNIT_ALIASES.items():
        if u in aliases:
            return canon, mult
    return u, mult


def words(text):
    text = re.sub(r"[^а-яё0-9\s]", " ", (text or "").lower())
    return {w for w in text.split() if len(w) > 2 and w not in STOPWORDS}


def polarity(text):
    toks = set(re.findall(r"[а-яё]+", (text or "").lower()))
    neg = any(any(t.startswith(w) for w in NEGATIVE_WORDS) for t in toks)
    pos = any(any(t.startswith(w) for w in POSITIVE_WORDS) for t in toks)
    if neg and not pos:
        return "neg"
    if pos and not neg:
        return "pos"
    return None


def material_conflict(a, b):
    ta = set(re.findall(r"[а-яё]+", (a or "").lower()))
    tb = set(re.findall(r"[а-яё]+", (b or "").lower()))
    am = {i for i, g in enumerate(MATERIAL_GROUPS) if ta & g}
    bm = {i for i, g in enumerate(MATERIAL_GROUPS) if tb & g}
    return bool(am) and bool(bm) and not (am & bm)


def numbers_in(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def best_match(name, unit, candidates, min_overlap=1, min_score=1.0):
    """ЕДИНИЦА ИЗМЕРЕНИЯ — ОБЯЗАТЕЛЬНОЕ условие, не бонус к скору.
    Живая находка (19.08): без этого требования короткое название вроде
    "Установка закладных деталей" (после стоп-слов — 2 значимых слова)
    проходило порог score>=1.0 по одним ключевым словам, а норма
    сопоставлялась с несовместимой единицей ("кг" сметы против нормы
    "за 1 шт. детали" СТО-ССР, где к тому же единица не сохранилась в
    источнике) — итог завышался в сотни раз (690 тыс. чел-час на одну
    позицию из 1,2 млн общего "итога"). Если единица нормы неизвестна
    или прямо несовместима с единицей сметной позиции — отклоняем
    совпадение целиком, не оцениваем "на глаз".
    """
    iw = words(name)
    iunit, _ = norm_unit_and_mult(unit)
    ipol = polarity(name)
    inums = numbers_in(name)
    best, best_score = None, 0.0
    for c in candidates:
        if not c["_words"] or not iw:
            continue
        if ipol and c["_pol"] and ipol != c["_pol"]:
            continue
        if material_conflict(name, c["_title"]):
            continue
        if iunit and c["_unit"] and iunit != c["_unit"]:
            continue
        if iunit and not c["_unit"]:
            continue  # единица нормы неизвестна — не считать объём на неё
        overlap = iw & c["_words"]
        if len(overlap) < min_overlap:
            continue
        score = len(overlap) / len(iw)
        if inums and c["_nums"] and (inums & c["_nums"]):
            score += 0.5
        if score > best_score:
            best_score, best = score, c
    if best is not None and best_score >= min_score:
        return best, round(best_score, 2)
    return None, 0.0


def main():
    smeta = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/vedomost_rd.json", encoding="utf-8"))
    ssr = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/ssr_spider_norms.json", encoding="utf-8"))
    gesn_groups = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/gesn_norms.json", encoding="utf-8"))

    ssr_cand = []
    for o in ssr:
        if o.get("labor_hours_per_unit") is None:
            continue
        ssr_cand.append({
            "_title": o["name"], "_words": words(o["name"]), "_pol": polarity(o["name"]),
            "_nums": numbers_in(o["name"]),
            "_unit": norm_unit_and_mult(o.get("unit"))[0],
            "code": o["code"], "name": o["name"], "unit": o.get("unit"),
            "hours_per_1": o["labor_hours_per_unit"], "source": "СТО-ССР",
        })

    gesn_cand = []
    for grp in gesn_groups:
        gt = (grp.get("group_title") or "").strip()
        for c in grp["codes"]:
            if c["hours_per_unit"] is None:
                continue
            title = f"{gt} {c['name']}" if gt and gt not in c["name"] else c["name"]
            unit, mult = norm_unit_and_mult(c.get("unit"))
            gesn_cand.append({
                "_title": title, "_words": words(title), "_pol": polarity(title),
                "_nums": numbers_in(title), "_unit": unit,
                "code": c["code"], "name": title, "unit": c.get("unit"),
                "hours_per_1": c["hours_per_unit"] / mult if mult else c["hours_per_unit"],
                "source": f"ГЭСН/{grp['sbornik']}",
            })

    results = []
    for s in smeta:
        match, score = best_match(s["name"], s.get("unit"), ssr_cand, min_score=1.0)
        src = "СТО-ССР"
        if match is None:
            match, score = best_match(s["name"], s.get("unit"), gesn_cand, min_score=1.3)
            src = "ГЭСН"
        row = {"n": s["n"], "name": s["name"], "unit": s.get("unit"), "qty": s.get("qty"),
               "matched_source": None, "matched_code": None, "matched_name": None,
               "hours_per_unit": None, "labor_hours_total": None, "score": None}
        if match is not None:
            qty = s.get("qty")
            try:
                qty = float(qty) if qty is not None else None
            except (TypeError, ValueError):
                qty = None
            row.update({
                "matched_source": src, "matched_code": match["code"], "matched_name": match["name"],
                "hours_per_unit": round(match["hours_per_1"], 4), "score": score,
                "labor_hours_total": round(qty * match["hours_per_1"], 2) if qty is not None else None,
            })
        results.append(row)

    out_path = "/home/oleg/Documents/TM-35/import/enir_work/smeta_normalized.json"
    json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    total = len(results)
    matched = sum(1 for r in results if r["matched_code"])
    ssr_n = sum(1 for r in results if r["matched_source"] == "СТО-ССР")
    gesn_n = sum(1 for r in results if r["matched_source"] == "ГЭСН")
    with_hours = sum(1 for r in results if r["labor_hours_total"] is not None)
    total_hours = sum(r["labor_hours_total"] for r in results if r["labor_hours_total"] is not None)

    print(f"Позиций сметы: {total}")
    print(f"Сопоставлено (СТО-ССР): {ssr_n} ({100*ssr_n/total:.1f}%)")
    print(f"Сопоставлено (ГЭСН, вспомогательный): {gesn_n} ({100*gesn_n/total:.1f}%)")
    print(f"Всего сопоставлено: {matched} ({100*matched/total:.1f}%)")
    print(f"С посчитанной трудоёмкостью (есть объём): {with_hours}")
    print(f"ИТОГО человеко-часов по сопоставленным позициям: {total_hours:,.0f}")


if __name__ == "__main__":
    main()
