"""
Расчёт человеко-часов на 163 работы ПТО (доделки, БД tm35) по
справочнику норм ЕНиР (enir_norms_v2.json — прямая оцифровка,
16 сборников, 572 §, реальные числа Н.вр.).

Метод сопоставления работа -> § — тот же принцип, что уже одобрен
координатором и проверен на первой попытке join со сметой (ключевые
слова наименования + единица измерения + явная проверка полярности
монтаж/демонтаж, чтобы не повторить баг "установка -> разборка").

Отличие от прежнего join: у найденного § может быть НЕСКОЛЬКО строк
(параметрическая норма — по диаметру/массе/способу). Если в названии
работы явно указано число (диаметр и т.п.) и оно однозначно совпадает
с одной из строк § — берём её трудозатраты. Если совпадения нет или
оно неоднозначно — ЧИСЛО НЕ ВЫДУМЫВАЕМ: работа помечается "требует
ручного выбора строки", все варианты § показываются рядом, чтобы
координатор/ПТО выбрал сам, глядя на реальный диаметр/способ этой
конкретной работы.
"""
import json
import re
from collections import defaultdict

STOPWORDS = {
    "и", "в", "с", "по", "для", "на", "от", "до", "из", "к", "не", "или",
    "при", "без", "под", "за", "работы", "работ", "устройство", "монтаж",
    "установка", "прочие", "конструкции", "конструкций", "стальных",
    "стальные", "стальной", "стальными", "элементов", "элементы",
    "конструктивных", "металлических", "металлоконструкций", "разборка",
    "разборки", "демонтаж", "демонтажа", "прокладка", "прокладки",
    "устройства", "выполнение", "производство",
}
MIN_OVERLAP = 1

NEGATIVE_WORDS = {"демонтаж", "разборка", "снятие", "удаление", "снос", "выемка"}
POSITIVE_WORDS = {"монтаж", "устройство", "установка", "укладка", "прокладка",
                   "сборка", "возведение", "строительство", "засыпка"}

UNIT_ALIASES = {
    "м3": {"м3", "м 3", "куб.м", "м³"},
    "м2": {"м2", "м 2", "кв.м", "м²"},
    "м": {"м", "пог.м", "п.м", "п.м."},
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


def polarity(text):
    toks = set(re.findall(r"[а-яё]+", (text or "").lower()))
    has_neg = any(any(t.startswith(w) for w in NEGATIVE_WORDS) for t in toks)
    has_pos = any(any(t.startswith(w) for w in POSITIVE_WORDS) for t in toks)
    if has_neg and not has_pos:
        return "neg"
    if has_pos and not has_neg:
        return "pos"
    return None


def words(text):
    text = (text or "").lower()
    text = re.sub(r"[^а-яё0-9\s]", " ", text)
    toks = [w for w in text.split() if len(w) > 2 and w not in STOPWORDS]
    return set(toks)


def unit_from_phrase(unit_phrase):
    if not unit_phrase:
        return None
    m = re.match(r"^\s*[\d,.]*\s*([а-яА-Я²³]+\.?\d?)", unit_phrase)
    return m.group(1) if m else None


def numbers_in(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def main():
    norms = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/enir_norms_v2.json", encoding="utf-8"))
    works = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/works_pto.json", encoding="utf-8"))

    paragraphs = defaultdict(lambda: {"rows": []})
    for r in norms:
        key = (r["sbornik"], r["code"])
        p = paragraphs[key]
        p["title"] = r["title"]
        p["unit_phrase"] = r.get("unit_phrase")
        p["crew_raw"] = r.get("crew_raw")
        if r["parsed"]:
            p["rows"].append({"condition": r.get("condition"), "hours_per_unit": float(r["hours_per_unit"])})

    para_list = []
    for (sbornik, code), p in paragraphs.items():
        if not p["rows"]:
            continue
        para_list.append({
            "sbornik": sbornik, "code": code, "title": p["title"],
            "unit_phrase": p["unit_phrase"], "crew_raw": p["crew_raw"],
            "rows": p["rows"],
            "_words": words(p["title"]),
            "_unit": norm_unit(unit_from_phrase(p["unit_phrase"])),
        })

    results = []
    for w in works:
        name = w["name"]
        unit = norm_unit(w.get("unit"))
        vol_raw = w.get("volume")
        try:
            volume = float(vol_raw) if vol_raw else None
        except ValueError:
            volume = None
        iw = words(name)
        item_pol = polarity(name)

        # Названия работ ПТО короткие (после чистки от стоп-слов часто
        # 1-3 значимых слова) — в отличие от сметы, где хватало Jaccard
        # с MIN_OVERLAP=2. Здесь считаем containment (доля СВОИХ слов
        # работы, найденных в § целиком) — иначе при iw из 1 слова
        # Jaccard-порог никогда не достижим (проверено: "Электромонтажные
        # работы по УТ 13" -> iw={'электромонтажные'}, 0 совпадений при
        # MIN_OVERLAP=2 на всех 572 §).
        best = None
        best_score = 0.0
        for p in para_list:
            if not p["_words"] or not iw:
                continue
            p_pol = polarity(p["title"])
            if item_pol and p_pol and item_pol != p_pol:
                continue
            overlap = iw & p["_words"]
            if len(overlap) < MIN_OVERLAP:
                continue
            score = len(overlap) / len(iw)
            if p["_unit"] and unit and p["_unit"] == unit:
                score += 0.25
            if score > best_score:
                best_score = score
                best = p

        row_out = {
            "id": w["id"], "code": w["code"], "name": name,
            "unit": w.get("unit"), "volume": volume, "status": w.get("status"),
            "enir_sbornik": None, "enir_code": None, "enir_title": None,
            "match_confidence": "не найдено", "match_score": round(best_score, 3),
            "condition": None, "hours_per_unit": None, "labor_hours": None,
            "candidates": None,
        }

        if best is not None and best_score >= 1.0:
            row_out["enir_sbornik"] = best["sbornik"]
            row_out["enir_code"] = best["code"]
            row_out["enir_title"] = best["title"]

            rows = best["rows"]
            if len(rows) == 1:
                chosen = rows[0]
                row_out["match_confidence"] = "найдена норма (единственный вариант)"
            else:
                name_nums = numbers_in(name)
                candidates = [r for r in rows if r["condition"] and (numbers_in(r["condition"]) & name_nums)]
                if len(candidates) == 1:
                    chosen = candidates[0]
                    row_out["match_confidence"] = "найдена норма (по параметру из названия)"
                else:
                    chosen = None
                    row_out["match_confidence"] = "требует ручного выбора строки (несколько вариантов)"
                    row_out["candidates"] = " | ".join(
                        f"{r['condition']}: {r['hours_per_unit']}" for r in rows[:15]
                    )
                    if len(rows) > 15:
                        row_out["candidates"] += f" | ... ещё {len(rows)-15}"

            if chosen:
                row_out["condition"] = chosen["condition"]
                row_out["hours_per_unit"] = chosen["hours_per_unit"]
                if volume is not None:
                    row_out["labor_hours"] = round(volume * chosen["hours_per_unit"], 2)

        results.append(row_out)

    out_path = "/home/oleg/Documents/TM-35/import/enir_work/labor_hours_pto.json"
    json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_total = len(results)
    n_computed = sum(1 for r in results if r["labor_hours"] is not None)
    n_manual = sum(1 for r in results if r["match_confidence"].startswith("требует"))
    n_none = sum(1 for r in results if r["match_confidence"] == "не найдено")
    total_hours = sum(r["labor_hours"] for r in results if r["labor_hours"] is not None)

    print(f"Всего работ: {n_total}")
    print(f"Посчитаны человеко-часы: {n_computed}")
    print(f"Найден § ЕНиР, но нужен ручной выбор параметра: {n_manual}")
    print(f"Норма не найдена: {n_none}")
    print(f"ИТОГО человеко-часов (по посчитанным {n_computed} работам): {total_hours:.1f}")


if __name__ == "__main__":
    main()
