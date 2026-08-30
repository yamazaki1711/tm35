"""
Свежий разбор parse_excel.py генерирует коды work по позиции строки в
файле (seq по счётчику листа) — при удалении/добавлении строк (16.08:
8 работ пропало, 2 появились по сравнению с боевой БД) это СМЕЩАЕТ seq
для всех последующих строк, и коды перестают совпадать с уже
существующими в БД для тех же самых по названию работ. Наивная загрузка
через load_to_postgres.py (upsert по `code`) создала бы дубликаты —
новую строку work без истории `daily_progress` вместо обновления
существующей.

Это скрипт сверки: сопоставляет новые 146 работ со старыми 152
(main+aux) ПО ТОЧНОМУ НАЗВАНИЮ (не по коду), переписывает `code` в
works.json/daily_progress.json на уже существующий код там, где
названия совпали (сохраняя id/историю), и минтит новые коды только для
реально новых названий. Печатает отчёт: что пропало, что появилось —
не молчит об этом.
"""
import json
from pathlib import Path

OUT_DIR = Path("/home/oleg/Documents/TM-35/import/output_1608")
OLD_TSV = "/tmp/old_work_main_aux_fresh.tsv"

MAX_MAIN = 105
MAX_AUX = 144


def main():
    old = []
    with open(OLD_TSV, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            old.append({"id": parts[0], "code": parts[1], "source": parts[2], "name": parts[3].strip()})
    old_by_name = {o["name"]: o for o in old}

    works = json.loads((OUT_DIR / "works.json").read_text(encoding="utf-8"))
    daily = json.loads((OUT_DIR / "daily_progress.json").read_text(encoding="utf-8"))

    code_map = {}  # generated_code -> resolved_code
    matched, minted = [], []
    next_main, next_aux = MAX_MAIN + 1, MAX_AUX + 1

    for w in works:
        gen_code = w["code"]
        old_row = old_by_name.get(w["name"].strip())
        if old_row:
            code_map[gen_code] = old_row["code"]
            w["code"] = old_row["code"]
            matched.append((old_row["code"], w["name"]))
        else:
            if w["source"] == "main":
                new_code = f"TM35-MAIN-{next_main:03d}"
                next_main += 1
            else:
                new_code = f"TM35-AUX-{next_aux:03d}"
                next_aux += 1
            code_map[gen_code] = new_code
            w["code"] = new_code
            minted.append((new_code, w["name"]))

    for d in daily:
        d["work_code"] = code_map[d["work_code"]]

    new_names = {w["name"].strip() for w in works}
    disappeared = [o for o in old if o["name"] not in new_names]

    (OUT_DIR / "works.json").write_text(json.dumps(works, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "daily_progress.json").write_text(json.dumps(daily, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Сопоставлено с существующими work (сохранён код и история): {len(matched)}")
    print(f"Новых работ (новый код, истории нет): {len(minted)}")
    for c, n in minted:
        print(f"  {c}: {n}")
    print(f"Пропало из нового файла (код/история в БД не тронуты, просто больше не обновляются): {len(disappeared)}")
    for o in disappeared:
        print(f"  {o['code']}: {o['name']}")


if __name__ == "__main__":
    main()
