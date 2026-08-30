"""
Шаг 1: нормализовать ведомость работ по РД (Смета контракта -ред.ДС№16
от 28.05.25.xlsx, 524 позиции, 36 разделов) в структурированный список
{раздел, номер_раздела, №_пп, наименование, ед_изм, объём}.

Секционные и служебные строки (заголовок раздела, "Итого по разделу",
"Сумма НДС", "Всего с НДС", шапка таблицы) распознаются и не попадают
в список позиций работ — тот же принцип, что уже применяется в проекте
для служебных строк Excel-графика ПТО (.claude/skills/tm35-excel).
"""
import json
import re
import openpyxl

SRC = "/home/oleg/Documents/TM-35/import/enir_work/smeta_kontrakta.xlsx"
OUT = "/home/oleg/Documents/TM-35/import/enir_work/vedomost_rd.json"

SKIP_PREFIXES = ("Итого по разделу", "Сумма НДС", "Всего с НДС", "в том числе")
SKIP_EXACT = {"Заказчик:", "м.п.", "Подрядчик:"}

wb = openpyxl.load_workbook(SRC, data_only=True)
ws = wb.active

items = []
current_section_num = None
current_section_name = None
subsection = None

skipped_service = []
skipped_other = []

for row in ws.iter_rows(min_row=1, values_only=True):
    cells = list(row)
    col0, col_name, col_unit, col_qty = cells[0], cells[1], cells[2], cells[3]

    if col0 is None and col_name is None:
        continue

    # Слитая (merged) строка-заголовок — текст лежит в первой ячейке
    # (cells[0]), остальные None. Разбираем два случая:
    # "Раздел N. <Название>" — новый раздел, иначе — подраздел.
    if isinstance(col0, str) and col_name is None and col_unit is None and col_qty is None:
        text = col0.strip()
        m = re.match(r"^Раздел\s+(\d+)\.\s*(.+)$", text)
        if m:
            current_section_num = int(m.group(1))
            current_section_name = m.group(2).strip()
            subsection = None
            continue
        if text.startswith(SKIP_PREFIXES) or text in SKIP_EXACT:
            skipped_service.append(text)
            continue
        subsection = text
        continue

    col_num = col0

    # Позиция работы: номер — число (или строка-число), наименование —
    # текст, объём — число. На листе номер хранится строкой ("1", "2", ...).
    n_val = None
    if isinstance(col_num, (int, float)):
        n_val = int(col_num)
    elif isinstance(col_num, str) and col_num.strip().isdigit():
        n_val = int(col_num.strip())

    if n_val is not None and isinstance(col_name, str):
        items.append({
            "n": n_val,
            "section_num": current_section_num,
            "section_name": current_section_name,
            "subsection": subsection,
            "name": col_name.strip(),
            "unit": (col_unit or "").strip() if isinstance(col_unit, str) else col_unit,
            "qty": col_qty,
        })
        continue

    # Шапка таблицы ("№п/п", "Наименование...", "1,2,3,4,5,6,7" и т.п.)
    # и всё, что не распознано — в отдельный список для проверки, не в items.
    skipped_other.append(cells[:6])

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=1)

print("items:", len(items))
print("sections:", len(set((i["section_num"], i["section_name"]) for i in items)))
print("skipped_service (sample):", skipped_service[:5], "... total", len(skipped_service))
print("skipped_other (non-item rows, review):")
for r in skipped_other:
    print(" ", r)
