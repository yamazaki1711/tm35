"""
Сверка кодов для загрузки из «Сводная Таблица по ТМ 35 от 28.08.26.xlsx»
(задание координатора 29.08.2026 — актуализировать всю систему, убрать
ИКС/РСК как категорию).

Тот же принцип, что reconcile_1608_codes.py: parse_excel_v2.py генерирует
коды по порядку строк в файле — они НЕ совпадают с уже существующими в
БД кодами тех же по названию работ. Сопоставляем по точному названию,
затем по нечёткому (только 4 явных случая с опечаткой/лишним словом,
проверены вручную — не общий fuzzy-матчинг по всей базе).

Отдельно — 6 случаев, где НОВАЯ строка совпадает с ДВУМЯ старыми (это и
есть находка задания: ИКС/РСК-работы в старых листах дублировали уже
существующие MAIN-работы под другим кодом) — берём код MAIN-варианта,
не минтим третий код.

Старые work-строки с source in ('iks','rsk') СЮДА не попадают вообще
(в новом файле нет отдельных ИКС/РСК секций) — их source меняется
отдельным SQL UPDATE после загрузки (см. reclassify_iks_rsk.sql),
эта загрузка их не трогает и не удаляет.
"""
import json
from pathlib import Path

OUT_DIR = Path("/home/oleg/Documents/TM-35/import/smr_280826/output")
OLD_TSV = "/tmp/claude-1000/-home-oleg/d973c3f1-942e-40b1-9945-4f57a68a3420/scratchpad/work_db_snapshot.psv"

# Текущие максимумы боевой БД (проверено запросом 29.08.2026) — новые
# коды минтятся строго ПОСЛЕ этих номеров, чтобы не столкнуться с уже
# существующими.
MAX_MAIN = 105
MAX_AUX = 146

# 4 явных нечётких совпадения (опечатка/лишний символ) — установлены
# вручную по score>=0.95 (difflib), не автоматическим порогом без проверки.
FUZZY_MAP = {
    "Водоборьба  от камер и КД": "Водооборьба  от камер и КД",
    "Упорядочивание материала на складе": "Упорядочивание материала на складе .",
    "Проверка электрооборудования в камерах, чистка контактов УТ 12, УТ11, УТ10":
        "Проверка электрооборудования в камерах, чистка контактов УТ 12, УТ11",
    "Перемонта обмотки импульсных трубок в камерах УУТЭ1; УУТЭ2, УТ5, УТ10":
        "Перемонта обмотки импульсных ктубок в камерах УУТЭ1; УУТЭ2, УТ5, УТ10",
}


def main():
    old = []
    with open(OLD_TSV, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 5:
                continue
            wid, code, source, status, name = parts[0], parts[1], parts[2], parts[3], "|".join(parts[4:])
            old.append({"id": wid, "code": code, "source": source, "status": status, "name": name.strip()})

    old_by_name = {}
    for o in old:
        old_by_name.setdefault(o["name"], []).append(o)

    works = json.loads((OUT_DIR / "works.json").read_text(encoding="utf-8"))
    daily = json.loads((OUT_DIR / "daily_progress.json").read_text(encoding="utf-8"))

    code_map = {}
    matched, minted, collisions = [], [], []
    next_main, next_aux = MAX_MAIN + 1, MAX_AUX + 1

    for w in works:
        gen_code = w["code"]
        name = w["name"].strip()
        cands = old_by_name.get(name)
        if not cands and name in FUZZY_MAP:
            cands = old_by_name.get(FUZZY_MAP[name])

        if cands:
            if len(cands) > 1:
                # коллизия: несколько старых кодов на одно название — берём
                # MAIN-вариант (не ИКС/РСК), это и есть дубликаты из задания.
                main_cands = [c for c in cands if c["source"] == "main"]
                chosen = main_cands[0] if main_cands else cands[0]
                collisions.append((chosen["code"], [c["code"] for c in cands], name))
            else:
                chosen = cands[0]
                matched.append((chosen["code"], name))
            code_map[gen_code] = chosen["code"]
            w["code"] = chosen["code"]
        else:
            if w["source"] == "main":
                new_code = f"TM35-MAIN-{next_main:03d}"
                next_main += 1
            else:
                new_code = f"TM35-AUX-{next_aux:03d}"
                next_aux += 1
            code_map[gen_code] = new_code
            w["code"] = new_code
            minted.append((new_code, name))

    for d in daily:
        d["work_code"] = code_map[d["work_code"]]

    new_names = {w["name"].strip() for w in works}
    new_names_with_fuzzy = set(new_names)
    for new_n, old_n in FUZZY_MAP.items():
        if new_n in new_names:
            new_names_with_fuzzy.add(old_n)
    disappeared = [o for o in old if o["name"] not in new_names_with_fuzzy]

    (OUT_DIR / "works.json").write_text(json.dumps(works, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "daily_progress.json").write_text(json.dumps(daily, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Сопоставлено 1-к-1 (сохранён код и история): {len(matched)}")
    print(f"Коллизии (несколько старых кодов на 1 новое название — взят MAIN): {len(collisions)}")
    for chosen, all_codes, name in collisions:
        print(f"  взято {chosen} из {all_codes}: {name[:70]}")
    print(f"Новых работ (новый код, истории нет): {len(minted)}")
    for c, n in minted:
        print(f"  {c}: {n}")
    print(f"Пропало из нового файла (код/история не тронуты, просто не обновляются): {len(disappeared)}")
    for o in disappeared:
        print(f"  {o['code']} [{o['source']}] {o['status']}: {o['name'][:70]}")

    # Отдельно: старые work.id с source in ('iks','rsk') — не в matched/collisions
    # ни разу как "сопоставлено", раз этот source не встречается в новом файле.
    reclassify_ids = [o["id"] for o in old if o["source"] in ("iks", "rsk")]
    print(f"\nК принудительной реклассификации source->'aux' (были iks/rsk): {len(reclassify_ids)}")
    print(" ", reclassify_ids)


if __name__ == "__main__":
    main()
