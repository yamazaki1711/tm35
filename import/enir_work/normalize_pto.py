"""
Тот же тест «можно ли нормировать», что и для сметы, но на новом снимке
Excel ПТО (16.08) — 56 из 146 работ имеют физическую единицу измерения
(не 'комп.') и объём. Переиспользует всю методологию normalize_smeta.py
(единица — обязательное условие, не бонус; конфликт материала;
полярность монтаж/демонтаж).
"""
import json

from normalize_smeta import (
    words, polarity, material_conflict, norm_unit_and_mult, best_match,
)


def main():
    pto = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/pto_1608.json", encoding="utf-8"))
    ssr = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/ssr_spider_norms.json", encoding="utf-8"))
    gesn_groups = json.load(open("/home/oleg/Documents/TM-35/import/enir_work/gesn_norms.json", encoding="utf-8"))

    physical = {"шт", "м3", "м2", "м", "м.п.", "п.м.", "т", "мп"}
    candidates = [i for i in pto if (i["unit"] or "").strip().lower() in physical and i.get("qty") not in (None, "")]

    ssr_cand = []
    for o in ssr:
        if o.get("labor_hours_per_unit") is None:
            continue
        ssr_cand.append({
            "_title": o["name"], "_words": words(o["name"]), "_pol": polarity(o["name"]),
            "_nums": set(__import__("re").findall(r"\d+(?:[.,]\d+)?", o["name"])),
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
                "_nums": set(__import__("re").findall(r"\d+(?:[.,]\d+)?", title)),
                "_unit": unit, "code": c["code"], "name": title, "unit": c.get("unit"),
                "hours_per_1": c["hours_per_unit"] / mult if mult else c["hours_per_unit"],
                "source": f"ГЭСН/{grp['sbornik']}",
            })

    results = []
    for it in candidates:
        match, score = best_match(it["name"], it["unit"], ssr_cand, min_score=1.0)
        src = "СТО-ССР"
        if match is None:
            match, score = best_match(it["name"], it["unit"], gesn_cand, min_score=1.3)
            src = "ГЭСН"
        row = {"n": it["n"], "name": it["name"], "unit": it["unit"], "qty": it["qty"],
               "matched_source": None, "matched_code": None, "matched_name": None,
               "hours_per_unit": None, "labor_hours_total": None, "score": None}
        if match is not None:
            qty = it["qty"]
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = None
            row.update({
                "matched_source": src, "matched_code": match["code"], "matched_name": match["name"],
                "hours_per_unit": round(match["hours_per_1"], 4), "score": score,
                "labor_hours_total": round(qty * match["hours_per_1"], 2) if qty is not None else None,
            })
        results.append(row)

    json.dump(results, open("/home/oleg/Documents/TM-35/import/enir_work/pto_normalized.json", "w", encoding="utf-8"),
               ensure_ascii=False, indent=1)

    matched = [r for r in results if r["matched_code"]]
    print(f"Работ ПТО с физической единицей: {len(candidates)}")
    print(f"Сопоставлено: {len(matched)}")
    for r in sorted(matched, key=lambda x: -(x["labor_hours_total"] or 0)):
        print(f"{r['n']:4d} {r['name'][:40]:40s} qty={r['qty']!s:<8} ед={r['unit']:<6} -> "
              f"[{r['matched_source']:8s}] {r['matched_code']:16s} {r['matched_name'][:32]:32s} "
              f"hrs/ед={r['hours_per_unit']:<7} ИТОГ={r['labor_hours_total']:,.1f}")


if __name__ == "__main__":
    main()
