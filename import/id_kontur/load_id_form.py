import json, os
import psycopg2
from psycopg2.extras import RealDictCursor

dsn = os.environ.get('TM35_DSN') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(dsn)
cur = conn.cursor(cursor_factory=RealDictCursor)

with open('/tmp/id_excel_parsed.json', encoding='utf-8') as f:
    data = json.load(f)

# Tabs whose reference data is visibly broken (copy-paste template, wrong
# labels — "Электрика"/"Мет констр" carrying each other's headers) — load
# with has_reference_block=false and a note, per TZ's own admission these
# need ПТО clarification; not fabricating better data for them.
BROKEN_TABS = {'elektrika', 'pavilyony', 'met_konstr'}

report = []

for order, t in enumerate(data['tabs'], start=1):
    if 'error' in t:
        report.append((t['code'], 'ERROR', t['error']))
        continue

    broken = t['code'] in BROKEN_TABS
    cur.execute(
        """insert into id_form_tab
           (code, label, has_section_level, display_order, source_sheet, has_reference_block, note)
           values (%s,%s,%s,%s,%s,%s,%s) returning id""",
        (t['code'], t['label'], t['has_section_level'], order, t['sheet'],
         not broken,
         'Заголовки колонок повреждены копипастом (перепутаны с др. вкладкой) — данные не заслуживают доверия, нужна сверка с ПТО' if broken else None),
    )
    tab_id = cur.fetchone()['id']

    st_count = 0
    for s in (t['statuses'] or []):
        cur.execute(
            """insert into id_form_status (tab_id, code, label, display_order, is_stopper)
               values (%s,%s,%s,%s,%s) on conflict (tab_id, code) do nothing""",
            (tab_id, s['code'], s['label'], s['order'],
             s['label'] in ('Нет проектного решения', 'Есть замечания', 'Нет лаборатории',
                            'Нет пр. реш.'.replace('.', ''), 'Замечания к площадке')),
        )
        st_count += 1

    wt_count = 0
    for w in t['work_types']:
        cur.execute(
            """insert into id_form_work_type (tab_id, name, display_order, responsible_name, signer_name, source_col)
               values (%s,%s,%s,%s,%s,%s) on conflict (tab_id, name) do nothing""",
            (tab_id, w['name'], w['order'], w['responsible'], w['signer'], w['col']),
        )
        wt_count += 1

    row_count = 0
    for r in t['rows']:
        cur.execute(
            """insert into id_form_row (tab_id, section_label, construction_label, foundation_label, source_row)
               values (%s,%s,%s,%s,%s)""",
            (tab_id, r['section'], r['construction'], r['foundation'], r['source_row']),
        )
        row_count += 1

    report.append((t['code'], 'OK', f'statuses={st_count} work_types={wt_count} rows={row_count} broken={broken}'))

conn.commit()

print("=== LOAD REPORT ===")
for code, status, info in report:
    print(f"{code:16s} {status:6s} {info}")

cur.execute("select count(*) as n from id_form_tab")
print("id_form_tab total:", cur.fetchone())
cur.execute("select count(*) as n from id_form_status")
print("id_form_status total:", cur.fetchone())
cur.execute("select count(*) as n from id_form_work_type")
print("id_form_work_type total:", cur.fetchone())
cur.execute("select count(*) as n from id_form_row")
print("id_form_row total:", cur.fetchone())
