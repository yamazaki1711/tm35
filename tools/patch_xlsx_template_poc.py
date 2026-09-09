#!/usr/bin/env python3
"""
Проверенный на реальном файле «01.09.26 График ИД Хабаровск с комм. ред.xlsx»
способ обновить данные в готовой книге Excel, НЕ разрушив её.

Прислан координатором 09.09.2026 (часть 4 задачи "Экспорт «График ИД»") —
техника, на которой основан _patch_grafik_id_sheet1()/export_id_grafik_xlsx()
в main.py. Здесь сохранён как справочный PoC, сам main.py использует
собственные функции (_xlsx_*) с той же логикой, не импортирует этот файл.

Проверено (см. раздел «Самопроверка» внизу): после патча ни одна часть
xlsx-архива не теряется, изменяются только те, которые мы намеренно
правим. Для сравнения: openpyxl load_workbook(...).save(...) на этом же
файле теряет 16 частей, включая xl/comments1.xml (57 КБ примечаний),
xl/comments2.xml (121 КБ), xl/threadedComments/threadedComment1.xml,
оба xl/drawings/vmlDrawing*.vml и все xl/printerSettings/*.bin —
то есть ровно то, что в названии файла обозначено как «с комм. ред.».

ПОЭТОМУ: openpyxl можно использовать ТОЛЬКО для чтения/проверки,
никогда для сохранения шаблона.

Запуск демо:  python3 patch_xlsx_template_poc.py template.xlsx out.xlsx
"""

import hashlib
import re
import sys
import zipfile

# ---------------------------------------------------------------- чтение

def read_parts(path):
    """Возвращает (порядок частей, словарь имя->байты). Порядок важен:
    сохраняем его при записи, чтобы диф архива был минимальным."""
    with zipfile.ZipFile(path) as z:
        order = z.namelist()
        return order, {n: z.read(n) for n in order}


def write_parts(path, order, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n in order:
            z.writestr(n, parts[n])


# ------------------------------------------- карта стилей по цвету заливки

def harvest_style_indices(styles_xml):
    """Собирает {цвет заливки -> [индексы стилей cellXfs]} из самого шаблона.

    Смысл: чтобы покрасить ячейку статуса в тот же зелёный, что в
    оригинале, не надо добавлять новый стиль в styles.xml (и рисковать
    рассинхроном) — в шаблоне уже есть ячейки с нужной заливкой, берём
    их индекс s= и переиспользуем. Цвет получается литерально тот же,
    вместе с рамками и шрифтом этого типа ячеек.

    На проверенном файле даёт:
      FF92D050 (зелёный «Подписано»)      -> [10, 18, 20, 31, 59, 62, ...]
      FFFFC000 (янтарный «на подписание») -> [60, 61, 64, 69, 70, 74, ...]
      FF3C1FCF (КЭВ)                      -> [75]
      theme6   (КРВ)                      -> [7, 8, 9, 17, 43, 45, ...]
    Хардкодить эти числа НЕ надо — пересобирать из шаблона при каждом
    запуске: правка шаблона человеком может их сдвинуть.
    """
    fills_block = re.search(r"<fills count=\"\d+\">(.*?)</fills>", styles_xml, re.S).group(1)
    fills = re.findall(r"<fill>.*?</fill>|<fill/>", fills_block, re.S)

    def colour_of(fill_idx):
        f = fills[fill_idx]
        m = re.search(r'fgColor rgb="([0-9A-Fa-f]{8})"', f)
        if m:
            return m.group(1).upper()
        m = re.search(r'fgColor theme="(\d+)"', f)
        return "theme" + m.group(1) if m else None

    xfs_block = re.search(r"<cellXfs count=\"\d+\">(.*?)</cellXfs>", styles_xml, re.S).group(1)
    xfs = re.findall(r"<xf [^>]*/>|<xf .*?</xf>", xfs_block, re.S)

    by_colour = {}
    for idx, xf in enumerate(xfs):
        m = re.search(r'fillId="(\d+)"', xf)
        if not m:
            continue
        c = colour_of(int(m.group(1)))
        if c:
            by_colour.setdefault(c, []).append(idx)
    return by_colour


# ------------------------------------------------------- патч ячеек листа

_CELL_RE = r'<c r="%s"(?P<attrs>[^>]*?)(?:/>|>.*?</c>)'


def _find_cell(sheet_xml, coord):
    m = re.search(_CELL_RE % coord, sheet_xml, re.S)
    if not m:
        raise KeyError(f"ячейка {coord} отсутствует в XML листа "
                       f"(в шаблоне у неё нет элемента <c>) — вставку "
                       f"новой ячейки делать отдельной функцией, "
                       f"с соблюдением порядка колонок внутри <row>")
    return m


def _style_of(attrs, override=None):
    if override is not None:
        return str(override)
    m = re.search(r's="(\d+)"', attrs)
    return m.group(1) if m else None


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def set_text(sheet_xml, coord, text, style=None):
    """Пишет строку в ячейку как inlineStr — sharedStrings.xml не трогаем
    вообще (иначе пришлось бы пересчитывать индексы всех строк книги).
    style=None — сохранить стиль, который был у ячейки в шаблоне."""
    m = _find_cell(sheet_xml, coord)
    s = _style_of(m.group("attrs"), style)
    s_attr = f' s="{s}"' if s is not None else ""
    new = (f'<c r="{coord}"{s_attr} t="inlineStr">'
           f'<is><t>{_xml_escape(text)}</t></is></c>')
    return sheet_xml[:m.start()] + new + sheet_xml[m.end():]


def set_number(sheet_xml, coord, value, style=None):
    m = _find_cell(sheet_xml, coord)
    s = _style_of(m.group("attrs"), style)
    s_attr = f' s="{s}"' if s is not None else ""
    new = f'<c r="{coord}"{s_attr}><v>{value}</v></c>'
    return sheet_xml[:m.start()] + new + sheet_xml[m.end():]


def set_formula(sheet_xml, coord, formula, style=None):
    """Кэшированное значение <v> намеренно НЕ пишем — его посчитает Excel
    при открытии (см. force_full_recalc)."""
    m = _find_cell(sheet_xml, coord)
    s = _style_of(m.group("attrs"), style)
    s_attr = f' s="{s}"' if s is not None else ""
    new = f'<c r="{coord}"{s_attr}><f>{_xml_escape(formula)}</f></c>'
    return sheet_xml[:m.start()] + new + sheet_xml[m.end():]


def clear_value(sheet_xml, coord, style=None):
    """Очищает значение, СОХРАНЯЯ оформление ячейки."""
    m = _find_cell(sheet_xml, coord)
    s = _style_of(m.group("attrs"), style)
    s_attr = f' s="{s}"' if s is not None else ""
    return sheet_xml[:m.start()] + f'<c r="{coord}"{s_attr}/>' + sheet_xml[m.end():]


def force_full_recalc(workbook_xml):
    """Мы убрали кэшированные значения у формул — просим Excel пересчитать
    книгу при открытии. Без этого возможен показ пустых ячеек до ручного
    F9 (и, в худшем случае, предложение «восстановить файл»)."""
    if "fullCalcOnLoad" in workbook_xml:
        return workbook_xml
    return re.sub(r"<calcPr ([^>]*?)/>", r'<calcPr \1 fullCalcOnLoad="1"/>',
                  workbook_xml)


# ------------------------------------------------------------ самопроверка

def assert_only_expected_changed(src_path, out_path, expected_changed):
    """Обязательный шаг перед отдачей файла пользователю: убедиться, что
    патч не тронул ничего лишнего и ничего не потерял."""
    with zipfile.ZipFile(src_path) as a, zipfile.ZipFile(out_path) as b:
        a_names, b_names = set(a.namelist()), set(b.namelist())
        lost = a_names - b_names
        if lost:
            raise AssertionError(f"потеряны части архива: {sorted(lost)}")
        changed = {
            n for n in a_names & b_names
            if hashlib.md5(a.read(n)).hexdigest() != hashlib.md5(b.read(n)).hexdigest()
        }
        unexpected = changed - set(expected_changed)
        if unexpected:
            raise AssertionError(f"изменены части, которые не должны были: "
                                 f"{sorted(unexpected)}")
    return sorted(changed)


CRITICAL_PARTS = [
    # то, что теряет openpyxl и что обязано выжить
    "xl/comments1.xml",
    "xl/comments2.xml",
    "xl/threadedComments/threadedComment1.xml",
    "xl/persons/person.xml",
    "xl/drawings/vmlDrawing1.vml",
    "xl/drawings/vmlDrawing2.vml",
    "xl/media/image1.png",
    "xl/externalLinks/externalLink1.xml",
    "xl/sharedStrings.xml",
]


def assert_critical_parts_present(out_path):
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    missing = [p for p in CRITICAL_PARTS if p not in names]
    if missing:
        raise AssertionError(f"в результате нет обязательных частей: {missing}")


# ------------------------------------------------------------------- демо

def demo(src, out):
    order, parts = read_parts(src)

    styles = harvest_style_indices(parts["xl/styles.xml"].decode("utf-8"))
    green = styles["FF92D050"][0]

    sheet = parts["xl/worksheets/sheet1.xml"].decode("utf-8")  # «График ИД»

    # статус: был янтарный «согласовано к подписанию» -> зелёный «Подписано 80%»
    sheet = set_text(sheet, "C22", "Подписано 80%", style=green)
    # метка Ганта переехала из H22 (июнь) в J22 (июль), формула как в шаблоне
    sheet = clear_value(sheet, "H22")
    sheet = set_formula(sheet, "J22", "E22")

    parts["xl/worksheets/sheet1.xml"] = sheet.encode("utf-8")
    parts["xl/workbook.xml"] = force_full_recalc(
        parts["xl/workbook.xml"].decode("utf-8")).encode("utf-8")

    write_parts(out, order, parts)

    changed = assert_only_expected_changed(
        src, out, {"xl/worksheets/sheet1.xml", "xl/workbook.xml"})
    assert_critical_parts_present(out)
    print("изменены части:", changed)
    print("все обязательные части на месте, ничего не потеряно")


if __name__ == "__main__":
    demo(sys.argv[1] if len(sys.argv) > 1 else "template.xlsx",
         sys.argv[2] if len(sys.argv) > 2 else "out.xlsx")
