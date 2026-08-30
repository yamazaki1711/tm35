"""
Шаг 3: сопоставить позиции ведомости работ по РД (vedomost_rd.json, 512
позиций) с параграфами норм ЕНиР (enir_norms.json). Детерминированное
сопоставление по пересечению значимых слов наименования + совместимость
единицы измерения — никакого угадывания LLM.

Три исхода на позицию (тот же принцип, что и data_quality_flag в БД
ТМ-35): точное (высокий score + единица совпала), эвристическое (score
выше порога, единица не проверена/не совпала) — эвристика, требует
проверки. Не найдено — по любой причине ниже порога.
"""
import json
import re

STOPWORDS = {
    "и", "в", "с", "по", "для", "на", "от", "до", "из", "к", "не", "или",
    "при", "без", "под", "за", "работы", "работ", "устройство", "монтаж",
    "установка", "прочие", "конструкции", "конструкций", "стальных",
    "стальные", "стальной", "стальными", "элементов", "элементы",
    "конструктивных", "металлических", "металлоконструкций", "разборка",
    "разборки", "демонтаж", "демонтажа", "прокладка", "прокладки",
    "устройства", "выполнение", "производство",
}
MIN_OVERLAP = 2

UNIT_ALIASES = {
    "м3": {"м3", "м 3", "куб.м", "м³"},
    "м2": {"м2", "м 2", "кв.м", "м²"},
    "м": {"м", "пог.м", "п.м"},
    "т": {"т", "тн"},
    "шт": {"шт", "шт.", "штук"},
    "компл": {"компл", "компл.", "комплект"},
}


def norm_unit(u):
    if not u:
        return None
    u = u.strip().lower().rstrip(".")
    for canon, aliases in UNIT_ALIASES.items():
        if u in {a.lower().rstrip(".") for a in aliases}:
            return canon
    return u


NEGATIVE_WORDS = {"демонтаж", "разборка", "снятие", "удаление", "снос", "выемка"}
POSITIVE_WORDS = {"монтаж", "устройство", "установка", "укладка", "прокладка",
                   "сборка", "возведение", "строительство", "засыпка"}


def polarity(text):
    # Токены целиком, не подстроки — иначе "демонтаж" ложно матчится
    # на "монтаж" как на подстроку и полярность "смешивается".
    toks = set(re.findall(r"[а-яё]+", (text or "").lower()))
    has_neg = any(any(t.startswith(w) for w in NEGATIVE_WORDS) for t in toks)
    has_pos = any(any(t.startswith(w) for w in POSITIVE_WORDS) for t in toks)
    if has_neg and not has_pos:
        return "neg"
    if has_pos and not has_neg:
        return "pos"
    return None  # смешано или не определено — не блокируем


def words(text):
    text = (text or "").lower()
    text = re.sub(r"[^а-яё0-9\s]", " ", text)
    toks = [w for w in text.split() if len(w) > 2 and w not in STOPWORDS]
    return set(toks)


def unit_from_phrase(unit_phrase):
    """Из 'unit_phrase' норм ЕНиР (напр. '100 м 3 грунта') вытащить
    единицу измерения саму по себе для сверки с ед.изм. сметы."""
    if not unit_phrase:
        return None
    m = re.match(r"^\s*[\d,.]*\s*([а-яА-Я²³]+\.?\d?)", unit_phrase)
    return m.group(1) if m else None


def main():
    smeta = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/vedomost_rd.json", encoding="utf-8"))
    norms = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/enir_norms.json", encoding="utf-8"))

    for n in norms:
        n["_words"] = words(n["title"])
        n["_unit"] = norm_unit(unit_from_phrase(n.get("unit_phrase")))

    results = []
    for item in smeta:
        iw = words(item["name"])
        item_unit = norm_unit(item.get("unit"))
        item_pol = polarity(item["name"])
        best = None
        best_score = 0.0
        for n in norms:
            if not n["_words"] or not iw:
                continue
            n_pol = polarity(n["title"])
            if item_pol and n_pol and item_pol != n_pol:
                continue  # монтаж vs демонтаж — заведомо разные операции
            overlap = iw & n["_words"]
            if len(overlap) < MIN_OVERLAP:
                continue
            score = len(overlap) / len(iw | n["_words"])
            if n["_unit"] and item_unit and n["_unit"] == item_unit:
                score += 0.25
            if score > best_score:
                best_score = score
                best = n

        if best is None or best_score < 0.35:
            status = "не найдено"
            match = None
        elif best_score >= 0.55 and best["_unit"] == item_unit:
            status = "точное"
            match = best
        else:
            status = "эвристическое"
            match = best

        results.append({
            **{k: v for k, v in item.items()},
            "match_status": status,
            "match_score": round(best_score, 3),
            "enir_code": match["code"] if match else None,
            "enir_sbornik": match["sbornik"] if match else None,
            "enir_title": match["title"] if match else None,
            "enir_unit_phrase": match["unit_phrase"] if match else None,
            "enir_crew_raw": match["crew_raw"] if match else None,
        })

    out_path = "/home/oleg/Documents/TM-35/import/enir_work/matched.json"
    json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_total = len(results)
    n_exact = sum(1 for r in results if r["match_status"] == "точное")
    n_heur = sum(1 for r in results if r["match_status"] == "эвристическое")
    n_none = sum(1 for r in results if r["match_status"] == "не найдено")
    print(f"Всего позиций: {n_total}")
    print(f"Точное: {n_exact} ({100*n_exact/n_total:.1f}%)")
    print(f"Эвристическое: {n_heur} ({100*n_heur/n_total:.1f}%)")
    print(f"Не найдено: {n_none} ({100*n_none/n_total:.1f}%)")


if __name__ == "__main__":
    main()
