# -*- coding: utf-8 -*-
"""Перезаливка СТРОК вкладок контура ИД — CSV от 27.08.2026
(TM35_stroki_vkladok_20260827.csv). Только id_form_row — справочники
(статусы/ответственные/виды работ), загруженные в прошлой задаче,
не трогаются.
"""
import csv
import os
import psycopg2
from psycopg2.extras import RealDictCursor

dsn = os.environ.get('TM35_DSN') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(dsn)
cur = conn.cursor(cursor_factory=RealDictCursor)

CSV_PATH = '/tmp/TM35_stroki_vkladok_20260827.csv'

TAB_MAP = {
    '1. ОПН': 'opn', '2. ОПН (рсм)': 'opn_rsm', 'ОПВ': 'opv', 'Н': 'n',
    'Изоляция': 'izolyaciya', 'Труба Сварка': 'truba_svarka', 'Лотки': 'lotki',
    'СОДК': 'sodk', 'Скользячки': 'skolzyachki', 'Камеры': 'kamery',
    'Колодцы': 'kolodcy', 'Траншеи': 'transhei', 'НО в лотках': 'no_v_lotkah',
    'Павильоны': 'pavilyony', 'Обвязка': 'obvyazka', 'Электрика': 'elektrika',
    'Мет констр': 'met_konstr',
}

# Уровни по вкладке (п.2 задания) — что из CSV идёт в section/construction/
# foundation. 'uchastok' — значение из колонки "Участок/фундамент" для
# вкладок "только участок" (Траншеи), кладём в construction_label (тот же
# смысл — единственный "листовой" идентификатор ряда, как и для "только
# конструкция" вкладок).
LEVELS = {
    'opn': 'full', 'opn_rsm': 'full', 'opv': 'full', 'n': 'full',
    'truba_svarka': 'section_construction', 'lotki': 'section_construction',
    'izolyaciya': 'section_only', 'sodk': 'section_only', 'obvyazka': 'section_only',
    'elektrika': 'section_only', 'met_konstr': 'section_only',
    'skolzyachki': 'construction_only', 'kamery': 'construction_only',
    'kolodcy': 'construction_only', 'no_v_lotkah': 'construction_only',
    'pavilyony': 'construction_only',
    'transhei': 'uchastok_only',
}

# НАХОДКА (п.3 задания): в CSV колонка "Раздел" для Электрика/Мет констр
# НЕ пустая, как было описано в задании, а содержит текст легенды статусов
# версии 26.08 ("Сделано в эл.виде", "Не сделано", "Работы не
# производятся", "Передано на проверку", "Подписано", "Нет проектного
# решения", "Есть замечания", "Нет лаборатории") — похоже, экспорт CSV для
# этих двух вкладок случайно захватил блок-легенду статусов, лежащую под
# таблицей, а не сами строки раздела. Это не то же самое, что "пусто".
# Не пишем эту легенду как название раздела (бессмысленно и вводит в
# заблуждение) — по вкладке показываем по номеру позиции, как и просили
# ("в форме такие строки показывать по номеру"), сырой текст сохраняем в
# note для трассировки, ничего не выдумываем взамен.
LEAKED_LEGEND_TABS = {'elektrika', 'met_konstr'}

# safety: подтвердить отсутствие живых ссылок перед truncate
cur.execute("select count(*) as n from id_form_entry")
entry_count = cur.fetchone()['n']
cur.execute("select count(*) as n from id_form_block")
block_count = cur.fetchone()['n']
if entry_count or block_count:
    print(f"СТОП: id_form_entry={entry_count}, id_form_block={block_count} — не truncate вслепую")
    raise SystemExit(1)

cur.execute("select id, code from id_form_tab")
tab_ids = {r['code']: r['id'] for r in cur.fetchall()}

with open(CSV_PATH, encoding='utf-8-sig') as f:
    reader = csv.DictReader(f, delimiter=';')
    csv_rows = list(reader)

cur.execute("truncate id_form_row restart identity cascade")

per_tab_counter = {}
loaded = {code: 0 for code in TAB_MAP.values()}

for r in csv_rows:
    tab_label = r['Вкладка'].strip()
    code = TAB_MAP.get(tab_label)
    if not code:
        print("НЕИЗВЕСТНАЯ ВКЛАДКА В CSV:", tab_label)
        continue
    tab_id = tab_ids.get(code)
    if not tab_id:
        print("НЕТ id_form_tab ДЛЯ КОДА:", code)
        continue

    per_tab_counter[code] = per_tab_counter.get(code, 0) + 1
    pos = per_tab_counter[code]

    razdel = r['Раздел'].strip() or None
    konstr = r['Конструкция'].strip() or None
    uchastok = r['Участок/фундамент'].strip() or None
    num = r['№ п/п'].strip() or None

    level = LEVELS[code]
    section_label = construction_label = foundation_label = None
    note = None

    if code in LEAKED_LEGEND_TABS:
        section_label = f"№{pos}"
        note = (f"Сырое значение колонки «Раздел» в CSV: {razdel!r} — совпадает с легендой "
                f"статусов версии 26.08, не название раздела (см. отчёт, находка §3). "
                f"№ п/п в исходнике: {num!r}.")
    elif level == 'full':
        section_label, construction_label, foundation_label = razdel, konstr, uchastok
    elif level == 'section_construction':
        section_label, construction_label = razdel, konstr
    elif level == 'section_only':
        section_label = razdel
    elif level == 'construction_only':
        construction_label = konstr
    elif level == 'uchastok_only':
        construction_label = uchastok

    cur.execute(
        """insert into id_form_row (tab_id, section_label, construction_label, foundation_label, source_row, note)
           values (%s,%s,%s,%s,%s,%s)""",
        (tab_id, section_label, construction_label, foundation_label, pos, note),
    )
    loaded[code] += 1

conn.commit()

EXPECTED = {
    'opn': 94, 'opn_rsm': 209, 'opv': 23, 'n': 46, 'izolyaciya': 21,
    'truba_svarka': 68, 'lotki': 2, 'sodk': 27, 'skolzyachki': 347,
    'kamery': 23, 'kolodcy': 25, 'transhei': 17, 'no_v_lotkah': 11,
    'pavilyony': 8, 'obvyazka': 30, 'elektrika': 8, 'met_konstr': 8,
}

print(f"{'код':16s}{'загружено':11s}{'заявлено':10s}")
total_loaded = 0
for code, exp in EXPECTED.items():
    got = loaded.get(code, 0)
    total_loaded += got
    flag = '' if got == exp else '  <-- РАСХОЖДЕНИЕ'
    print(f"{code:16s}{got:<11d}{exp:<10d}{flag}")
print("ИТОГО:", total_loaded, "/ 967 заявлено")

cur.execute("select count(*) as n from id_form_row")
print("id_form_row total:", cur.fetchone())
