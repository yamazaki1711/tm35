import os
import statistics
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

TM35_APP_PW = open("/home/oleg/Documents/TM-35/.secrets/tm35_app.env").read().split("tm35_app:")[1].split("@")[0]
DSN = f"postgresql://tm35_app:{TM35_APP_PW}@127.0.0.1:15432/tm35"

conn = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute(
    """
    select work_id, date, planned_crew
    from daily_progress
    where source='excel_import' and planned_crew is not null and planned_crew > 0
    order by work_id, date
    """
)
rows = cur.fetchall()

by_work = {}
for r in rows:
    by_work.setdefault(r["work_id"], []).append(r["date"])

print(f"Работ с планом > 0 хотя бы на 1 день: {len(by_work)}")

# ---- Тест 1+2: непрерывность и фрагментация (выходные не считаются разрывом) ----
def is_weekend(d):
    return d.weekday() >= 5  # 5=Сб, 6=Вс

def count_blocks(dates):
    dates = sorted(dates)
    blocks = 1
    for i in range(1, len(dates)):
        gap_days = (dates[i] - dates[i - 1]).days
        if gap_days <= 1:
            continue
        # проверяем, все ли дни между ними — выходные
        all_weekend = all(
            is_weekend(dates[i - 1] + timedelta(days=k))
            for k in range(1, gap_days)
        )
        if not all_weekend:
            blocks += 1
    return blocks

blocks_per_work = {wid: count_blocks(ds) for wid, ds in by_work.items()}
contiguous = sum(1 for b in blocks_per_work.values() if b == 1)
print(f"\n=== Тест 1: непрерывность ===")
print(f"Работ с одним сплошным блоком (без выходных-разрывов): {contiguous} из {len(by_work)} ({100*contiguous/len(by_work):.1f}%)")

print(f"\n=== Тест 2: фрагментация ===")
vals = list(blocks_per_work.values())
print(f"Среднее число блоков на работу: {statistics.mean(vals):.2f}")
print(f"Медианное число блоков на работу: {statistics.median(vals):.1f}")
print(f"Распределение: {sorted(vals)[:10]} ... {sorted(vals)[-10:]}")
print(f"Максимум блоков у одной работы: {max(vals)} (work_id={max(blocks_per_work, key=blocks_per_work.get)})")

# ---- Тест 4: трудоёмкость vs объём ----
cur.execute("select id, volume from work")
vol_by_id = {r["id"]: float(r["volume"]) if r["volume"] is not None else None for r in cur.fetchall()}
trudoemkost = {wid: sum(1 for _ in ds) for wid, ds in by_work.items()}  # заменим на сумму planned_crew ниже
cur.execute(
    "select work_id, sum(planned_crew) as t from daily_progress where source='excel_import' and planned_crew is not null group by work_id"
)
trudoemkost = {r["work_id"]: float(r["t"]) for r in cur.fetchall()}

pairs = [(trudoemkost[wid], vol_by_id[wid]) for wid in trudoemkost if vol_by_id.get(wid) is not None and vol_by_id[wid] > 0]
print(f"\n=== Тест 4: трудоёмкость vs объём ===")
print(f"Пар (трудоёмкость, объём>0) для сравнения: {len(pairs)} из {len(trudoemkost)}")
if len(pairs) >= 3:
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    corr = cov / (sx * sy) if sx > 0 and sy > 0 else None
    print(f"Коэффициент корреляции Пирсона (трудоёмкость, объём): {corr}")
else:
    print("Недостаточно данных для корреляции.")

# ---- Тест 5: край горизонта планирования ----
last_day_per_work = {wid: max(ds) for wid, ds in by_work.items()}
from collections import Counter
cnt = Counter(last_day_per_work.values())
print(f"\n=== Тест 5: край горизонта планирования ===")
print("Топ-10 самых частых дат последнего планового дня:")
for d, n in cnt.most_common(10):
    print(f"  {d}: {n} работ ({100*n/len(last_day_per_work):.1f}%)")

first_day_per_work = {wid: min(ds) for wid, ds in by_work.items()}
cnt_first = Counter(first_day_per_work.values())
print("Топ-5 самых частых дат ПЕРВОГО планового дня (для сравнения):")
for d, n in cnt_first.most_common(5):
    print(f"  {d}: {n} работ")

cur.close()
conn.close()
