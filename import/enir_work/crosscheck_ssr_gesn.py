"""
Сверка СТО-ССР (Spider Project, основной справочник) с ГЭСН
(вспомогательный) — по прямому указанию координатора: раз Spider
Project вероятно подключён к ГЭСН через API, стоит сравнить числа
трудоёмкости там, где операции пересекаются.

Метод — тот же принцип сопоставления, что уже проверен в проекте
(ключевые слова названия + единица измерения + явная проверка
полярности монтаж/демонтаж, плюс сверка числового параметра — диаметра
— когда он есть в названии), но здесь это ТОЛЬКО сверка для отчёта, не
запись в БД — ниже планка допустима, чем при автосвязывании.
"""
import json
import re
from collections import defaultdict

STOPWORDS = {
    "и", "в", "с", "по", "для", "на", "от", "до", "из", "к", "не", "или",
    "при", "без", "под", "за", "работы", "работ", "устройство", "монтаж",
    "установка", "прочие", "конструкции", "конструкций", "трубы",
    "трубопроводов", "труб",
}
# Материал/технология — НЕ стоп-слова (в отличие от прежних сопоставлений
# в проекте, где "стальных" было слишком общим фоном): здесь именно
# материал часто и есть ключевое различие (сталь vs ПЭ — совсем разные
# операции сварки). Живой пример найденной ошибки: "Сварка стальной
# трубы д325-500" сопоставилось с "Сварка полиэтиленовых труб встык" —
# только из-за того, что "стальной"/"полиэтиленовых" были в стоп-словах.
MATERIAL_GROUPS = [
    {"стальной", "стальная", "стальные", "сталь", "стали"},
    {"полиэтиленовых", "полиэтиленовой", "пэ", "пвх", "пластиковых", "пластикового"},
    {"чугунных", "чугунный", "чугуна"},
    {"железобетонных", "жб", "бетонных", "бетона"},
]


def material_conflict(text_a, text_b):
    ta = set(re.findall(r"[а-яё]+", (text_a or "").lower()))
    tb = set(re.findall(r"[а-яё]+", (text_b or "").lower()))
    a_mats = {i for i, g in enumerate(MATERIAL_GROUPS) if ta & g}
    b_mats = {i for i, g in enumerate(MATERIAL_GROUPS) if tb & g}
    return bool(a_mats) and bool(b_mats) and not (a_mats & b_mats)
NEGATIVE_WORDS = {"демонтаж", "разборка", "снятие", "удаление", "снос", "выемка"}
POSITIVE_WORDS = {"монтаж", "устройство", "установка", "укладка", "прокладка",
                   "сборка", "возведение", "строительство", "засыпка", "сварка"}

UNIT_ALIASES = {
    "м3": {"м3", "м 3", "куб.м"}, "м2": {"м2", "м 2", "кв.м"},
    "м": {"м", "мп", "пог.м", "п.м"}, "т": {"т", "тн"}, "шт": {"шт", "шт."},
    "стык": {"стык"}, "рез": {"рез"}, "компл": {"компл", "комп", "комплект"},
}


UNIT_MULT_RE = re.compile(r"^(\d[\d\s]*)\s*(.+)$")


def norm_unit_and_mult(u):
    """ГЭСН публикует нормы на базис 100/1000 единиц ('100 м3', '1000 м3',
    '10 соединений'), СТО-ССР — на 1 единицу ('м2','шт'). Без разделения
    множителя и единицы прямое сравнение чисел вводит в заблуждение —
    живой пример находки: "Установка металлических столбов" сначала
    выглядела как расхождение в 30+ раз, хотя единица ГЭСН была "100 шт"
    против "шт" у СТО-ССР. Возвращает (базовая_единица, множитель)."""
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


def norm_unit(u):
    return norm_unit_and_mult(u)[0]


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


def numbers_in(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def main():
    ssr = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/ssr_spider_norms.json", encoding="utf-8"))
    gesn_groups = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/gesn_norms.json", encoding="utf-8"))

    # gesn_norms.json сгруппирован по таблицам (parse_gesn.py) — раскрыть
    # в плоский список по кодам, как ожидает остальной код этого скрипта.
    gesn = []
    for grp in gesn_groups:
        group_title = (grp.get("group_title") or "").strip()
        for c in grp["codes"]:
            if c["hours_per_unit"] is None:
                continue
            name = c["name"]
            full_title = f"{group_title} {name}" if group_title and group_title not in name else name
            gesn.append({
                "sbornik": grp["sbornik"], "code": c["code"], "title": full_title,
                "unit_phrase": c.get("unit"), "condition": None,
                "hours_per_unit": c["hours_per_unit"],
            })
    for g in gesn:
        g["_words"] = words(g["title"])
        g["_unit"], g["_mult"] = norm_unit_and_mult(g.get("unit_phrase"))
        # приведённая к 1 единице норма — то, с чем реально сравнима
        # СТО-ССР (которая всегда на 1 единицу измерения)
        g["_hours_per_1"] = g["hours_per_unit"] / g["_mult"] if g["_mult"] else g["hours_per_unit"]
        g["_nums"] = numbers_in(g["title"])

    results = []
    for o in ssr:
        if o.get("labor_hours_per_unit") is None:
            continue
        iw = words(o["name"])
        iunit = norm_unit(o.get("unit"))
        ipol = polarity(o["name"])
        inums = numbers_in(o["name"])

        best = None
        best_score = 0.0
        for g in gesn:
            if not g["_words"] or not iw:
                continue
            gpol = polarity(g["title"])
            if ipol and gpol and ipol != gpol:
                continue
            if material_conflict(o["name"], g["title"]):
                continue
            overlap = iw & g["_words"]
            if len(overlap) < 1:
                continue
            score = len(overlap) / len(iw)
            if g["_unit"] and iunit and g["_unit"] == iunit:
                score += 0.3
            if inums and g["_nums"] and (inums & g["_nums"]):
                score += 0.5
            if score > best_score:
                best_score = score
                best = g

        if best is not None and best_score >= 1.0:
            gesn_hours_per_1 = best["_hours_per_1"]
            ratio = o["labor_hours_per_unit"] / gesn_hours_per_1 if gesn_hours_per_1 else None
            results.append({
                "ssr_code": o["code"], "ssr_name": o["name"], "ssr_unit": o.get("unit"),
                "ssr_hours": o["labor_hours_per_unit"],
                "gesn_sbornik": best["sbornik"], "gesn_code": best["code"], "gesn_title": best["title"],
                "gesn_unit_raw": best.get("unit_phrase"), "gesn_hours_raw": best["hours_per_unit"],
                "gesn_hours_per_1unit": round(gesn_hours_per_1, 4) if gesn_hours_per_1 else None,
                "score": round(best_score, 2), "ratio": round(ratio, 2) if ratio else None,
            })

    out_path = "/home/oleg/Documents/TM-35/import/enir_work/ssr_vs_gesn_crosscheck.json"
    json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n = len(results)
    close = sum(1 for r in results if r["ratio"] and 0.7 <= r["ratio"] <= 1.4)
    far = [r for r in results if r["ratio"] and not (0.5 <= r["ratio"] <= 2.0)]

    print(f"СТО-ССР операций с трудоёмкостью: {sum(1 for o in ssr if o.get('labor_hours_per_unit') is not None)}")
    print(f"Найдена сопоставимая норма ГЭСН: {n}")
    print(f"Близкое совпадение (0.7-1.4x): {close} ({100*close/n:.0f}%)" if n else "")
    print(f"Сильное расхождение (>2x или <0.5x): {len(far)}")
    print()
    for r in sorted(results, key=lambda x: -(x["ratio"] or 0) if (x["ratio"] or 0) > 2 else (1/x["ratio"] if x["ratio"] else 0))[:0]:
        pass
    print("=== Все сопоставления, отсортировано по score (для ручной проверки) ===")
    for r in sorted(results, key=lambda x: -x["score"]):
        print(f"{r['score']:.2f} | {r['ssr_code']:20s} {r['ssr_name'][:35]:35s} сср={r['ssr_hours']:<8} "
              f"гэсн(на 1 ед)={r['gesn_hours_per_1unit']:<9} x{r['ratio']:<6} | {r['gesn_code']} {r['gesn_title'][:40]}")


if __name__ == "__main__":
    main()
