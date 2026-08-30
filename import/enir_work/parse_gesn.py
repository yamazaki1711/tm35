"""
Извлечение норм ГЭСН (2022, Минстрой) из PDF, сконвертированных в текст
через `pdftotext -layout` (сохраняет колоночное выравнивание таблиц).

Модель данных ГЭСН отличается от ЕНиР и не требует угадывания
параметров по тексту таблицы:
  - "Таблица ГЭСН NN-RR-TTT <название>" — заголовок группы норм.
  - Перед самой ресурсной таблицей идёт ЛИСТИНГ "код — наименование"
    (иногда с групповым заголовком-подсказкой над несколькими кодами,
    напр. "Устройство бетонных фундаментов ... под колонны объемом:" ->
    "06-01-001-02  до 3 м3") — это и даёт готовое человекочитаемое
    название каждого кода, ничего домысливать не нужно.
  - "Измеритель: <единица>" — база измерения для всех кодов таблицы.
  - Ресурсная таблица может повторяться несколько раз (постранично) —
    каждый повтор даёт очередную порцию кодов-колонок ("Код ресурса |
    Наименование | Ед.изм. | <код1> <код2> ...").
  - Трудозатраты чел-час = "1-100-XX Средний разряд работы X,X" (рабочие)
    + "Затраты труда машинистов" (машинисты), суммарно по каждому коду —
    обе строки размечены явно, не нужно решать, какая часть таблицы
    относится к людям, а какая к машинам.

Сопоставление число -> код делается по БЛИЖАЙШЕЙ колонке (позиция
символа в строке), а не по порядку — пустые ячейки в ГЭСН часты, чисто
позиционное сопоставление с допуском отражает реальное выравнивание
pdftotext -layout надёжнее, чем угадывание по порядковому номеру.
"""
import json
import re
from pathlib import Path

CODE_RE = re.compile(r"^(\d{2}-\d{2}-\d{3}-\d{2})\s+(.+)$")
TABLE_START_RE = re.compile(r"^Таблица ГЭСН[а-я]? (\d{2}-\d{2}-\d{3})")
MEASURER_RE = re.compile(r"Измеритель:\s*(.+)")
RESOURCE_HEADER_RE = re.compile(r"^\s*Код ресурса\s+Наименование элемента затрат\s+Ед\. изм\.\s+(.*)$")
CODE_FRAGMENT_RE = re.compile(r"(?:\d{2}-\d{2}-)?\d{3}-\d{2}")
WORKER_ROW_RE = re.compile(r"^1-100-\d+\s+Средний разряд работы[^\d]*[\d,.]+\s+чел\.-ч\s+(.*)$")
MACHINIST_ROW_RE = re.compile(r"^\s*2\s+Затраты труда машинистов\s+чел\.-ч\s+(.*)$")
NUMBER_TOKEN_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def is_spaced_out(line):
    """Декоративный заголовок вида 'Т а бл иц а ГЭ СН 0 6- 01' — пропускаем.
    Считаем долю пробелов ТОЛЬКО внутри содержимого (после strip) — иначе
    обычная строка с длинным отступом слева (двухколоночная вёрстка PDF,
    правый столбец) ложно распознаётся как декоративная только из-за
    отступа, не из-за реального межбуквенного разрежения (живой баг,
    найден на Сборнике 6, таблица 06-01-002 — терялась половина
    названия)."""
    content = line.strip()
    if not content:
        return False
    letters = re.sub(r"\s", "", content)
    return len(letters) > 0 and (len(content) - len(letters)) / len(content) > 0.35


def parse_document(text, sbornik_name):
    lines = text.split("\n")
    groups = []
    i = 0
    n = len(lines)

    while i < n:
        m = TABLE_START_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        table_code_prefix = m.group(1)
        i += 1
        # пропустить декоративный дублирующий заголовок и название
        while i < n and (is_spaced_out(lines[i]) or not lines[i].strip()):
            i += 1
        title_lines = []
        while i < n and lines[i].strip() and not lines[i].strip().startswith(("Состав работ", "Измеритель")) \
                and not CODE_RE.match(lines[i].strip()) and not TABLE_START_RE.match(lines[i].strip()):
            title_lines.append(lines[i].strip())
            i += 1
        # "Состав работ:" иногда делит физическую строку с ХВОСТОМ
        # названия таблицы (двухколоночная вёрстка PDF — правый столбец
        # донёс последнее слово названия на ту же строку, что и левый
        # "Состав работ:") — не терять этот хвост (живой пример: "печи"
        # в Сборнике 6, 06-01-002 "...трубы и доменные [печи]").
        if i < n and lines[i].strip().startswith("Состав работ"):
            tail = lines[i].strip()[len("Состав работ"):].lstrip(":").strip()
            if tail:
                title_lines.append(tail)
        group_title = " ".join(title_lines).strip()

        codes = {}  # code -> name
        current_unit = None
        current_prefix_ctx = None
        resource_rows = []  # (code -> value) накопительно по всем повторам таблицы

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if TABLE_START_RE.match(stripped):
                break

            mm = MEASURER_RE.search(stripped)
            if mm:
                current_unit = mm.group(1).strip()
                i += 1
                continue

            cm = CODE_RE.match(stripped)
            if cm:
                code, name = cm.group(1), cm.group(2).strip()
                full_name = f"{current_prefix_ctx} {name}" if current_prefix_ctx else name
                codes[code] = full_name.strip()
                i += 1
                continue

            rh = RESOURCE_HEADER_RE.match(line)
            if rh:
                header_rest = line[len(line) - len(line.lstrip()):]
                # позиции кодовых фрагментов в ИСХОДНОЙ строке (важно для
                # сопоставления с числами в строках данных ниже)
                frag_positions = [(mfr.start(), mfr.group()) for mfr in CODE_FRAGMENT_RE.finditer(line)]
                # разрешить каждый фрагмент к полному коду по совпадению
                # хвоста среди уже известных кодов таблицы
                col_map = []  # (position, full_code)
                for pos, frag in frag_positions:
                    suffix = frag[-6:]  # "NNN-NN"
                    candidates = [c for c in codes if c.endswith(suffix)]
                    if len(candidates) == 1:
                        col_map.append((pos, candidates[0]))
                    elif len(candidates) > 1:
                        # неоднозначно — берём код с ближайшим префиксом сборника/раздела
                        best = min(candidates, key=lambda c: abs(len(c) - len(frag)))
                        col_map.append((pos, best))
                i += 1

                # следующая строка может быть ПРОДОЛЖЕНИЕМ кодов (перенос
                # "06-01-" / "001-01" на двух строках) — если очередная
                # строка сама похожа на ещё один ряд числовых фрагментов
                # без кириллицы и не начинается с "1"/"2"/"3"/"4" секции,
                # объединяем позиционно (сопоставляем по ближайшей позиции).
                if i < n and CODE_FRAGMENT_RE.search(lines[i]) and not re.search(r"[а-яА-Я]{3}", lines[i]):
                    frag2 = [(mfr.start(), mfr.group()) for mfr in CODE_FRAGMENT_RE.finditer(lines[i])]
                    for pos2, frag2v in frag2:
                        merged = None
                        for pos1, frag1v in frag_positions:
                            if abs(pos1 - pos2) <= 3:
                                merged = frag1v + frag2v
                                break
                        if merged:
                            suffix = merged[-6:]
                            candidates = [c for c in codes if c.endswith(suffix)]
                            if len(candidates) == 1:
                                col_map = [(p, cd) for p, cd in col_map if abs(p - pos2) > 3]
                                col_map.append((pos2, candidates[0]))
                    i += 1

                continue

            wm = WORKER_ROW_RE.match(stripped)
            if wm and col_map:
                nums = [(m2.start(), m2.group()) for m2 in NUMBER_TOKEN_RE.finditer(line)]
                for pos, tok, code in _assign_nearest(nums, col_map):
                    try:
                        resource_rows.append(("worker", code, float(tok.replace(",", "."))))
                    except ValueError:
                        pass
                i += 1
                continue

            mm2 = MACHINIST_ROW_RE.match(line)
            if mm2 and col_map:
                nums = [(m2.start(), m2.group()) for m2 in NUMBER_TOKEN_RE.finditer(line)]
                for pos, tok, code in _assign_nearest(nums, col_map):
                    try:
                        resource_rows.append(("machinist", code, float(tok.replace(",", "."))))
                    except ValueError:
                        pass
                i += 1
                continue

            # групповой заголовок-подсказка (заканчивается на ':', без кода)
            # — значим только ПОСЛЕ "Измеритель:" (в самом "Составе работ"
            # тоже встречаются строки на ':', это не то же самое, см.
            # находку: "Для норм 06-01-001-14, 06-01-001-21:" ложно
            # попадало в название кода 06-01-001-01).
            if current_unit is not None and stripped.endswith(":") and not CODE_RE.match(stripped) \
                    and len(stripped) > 3 and not stripped.startswith(("Код ресурса",)):
                current_prefix_ctx = stripped.rstrip(":")
                i += 1
                continue

            i += 1

        if codes:
            hours = {}
            for kind, code, val in resource_rows:
                hours[code] = hours.get(code, 0.0) + val
            groups.append({
                "sbornik": sbornik_name,
                "table_prefix": table_code_prefix,
                "group_title": group_title,
                "codes": [
                    {
                        "code": code,
                        "name": codes[code],
                        "unit": current_unit,
                        "hours_per_unit": round(hours[code], 4) if code in hours else None,
                    }
                    for code in codes
                ],
            })

    return groups


def _assign_nearest(nums, col_map, tol=8):
    """Сопоставление ОТ ЧИСЛА к ближайшему коду-колонке (не наоборот) —
    иначе в разреженной строке (не у каждого кода есть значение) одно и
    то же число может попасть сразу нескольким соседним кодам, если
    сопоставлять от кода к ближайшему числу (проверено на баге:
    06-01-001-01 "135" ошибочно приписывалось и коду 06-01-001-02).
    Каждое число уходит РОВНО одному, действительно ближайшему коду."""
    result = []
    for pos, tok in nums:
        best_code = None
        best_d = tol + 1
        for cpos, code in col_map:
            d = abs(cpos - pos)
            if d < best_d:
                best_d = d
                best_code = code
        if best_code is not None:
            result.append((pos, tok, best_code))
    return result


def main():
    pdf_dir = Path("/home/oleg/Documents/TM-35/import/enir_work/gesn_pdf")
    out_path = Path("/home/oleg/Documents/TM-35/import/enir_work/gesn_norms.json")

    all_groups = []
    stats = []
    for txt_path in sorted(pdf_dir.glob("*.txt")):
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        groups = parse_document(text, txt_path.stem)
        all_groups.extend(groups)
        n_codes = sum(len(g["codes"]) for g in groups)
        n_with_hours = sum(1 for g in groups for c in g["codes"] if c["hours_per_unit"] is not None)
        stats.append((txt_path.stem, len(groups), n_codes, n_with_hours))

    out_path.write_text(json.dumps(all_groups, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{'Файл':40s} {'групп':>7s} {'кодов':>7s} {'с нормой':>9s}")
    total_codes = total_hours = 0
    for name, n_groups, n_codes, n_with_hours in stats:
        print(f"{name:40s} {n_groups:>7d} {n_codes:>7d} {n_with_hours:>9d}")
        total_codes += n_codes
        total_hours += n_with_hours
    print(f"\nВсего кодов: {total_codes}, с извлечённой нормой чел-час: {total_hours} "
          f"({100*total_hours/total_codes:.1f}%)" if total_codes else "нет кодов")


if __name__ == "__main__":
    main()
