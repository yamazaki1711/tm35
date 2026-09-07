"""Накопление погоды в одной константной точке объекта.

Координаты объекта не привязаны к работе/участку/опоре — труба физически
неподвижна, точность в сотни метров для погоды значения не имеет (решение
координатора, см. задачу про daily_weather). Координаты читаются из
app_setting (object_lat/object_lon), а не хардкодятся здесь, чтобы их можно
было поменять без правки кода.

ВЕРСИЯ v2 (29.08.2026, задание координатора): раньше хранились ТОЛЬКО
суточные агрегаты (precipitation_sum за день, windspeed_10m_max,
temperature_2m_min/max) — это не то же самое, что погода на начало смены.
Координатор: /report должен показывать замер на 09:00 УТРА по времени
ОБЪЕКТА (Хабаровск, UTC+10), не суточный итог. Теперь запрашивается
почасовой ряд (hourly) ДОПОЛНИТЕЛЬНО к суточному (daily НЕ убран — те
колонки уже наполнены за 01.07-28.08 и могут понадобиться, правило
проекта не удалять заполненные данные), и из часового ряда берётся
запись ровно на 09:00 МЕСТНОГО времени объекта. `timezone=` в запросе —
явное значение из app_setting.object_timezone, не "auto" и не UTC/MSK
(тоже прямое требование — не полагаться на угадывание Open-Meteo по
координатам, хотя для Хабаровска оно и совпало бы).

Режимы:
  --mode backfill --start YYYY-MM-DD --end YYYY-MM-DD
      Historical API одним запросом на весь диапазон. Для первичного
      наполнения истории.
  --mode daily
      Forecast API за вчера и сегодня одним запросом. Для cron.

Идемпотентно: upsert по (date). Не смогли получить день — строка со
status='error' и текстом причины, а не тишина и не выдуманные нули.
Если суточные данные за день есть, а часовых на 09:00 нет — поля
_09 остаются NULL, суточные ('дневные') колонки НЕ используются как
подмена (см. задание — "не подставлять суточный агрегат молча").
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
from db import query_one, execute  # noqa: E402

DAILY_FIELDS = (
    "precipitation_sum,precipitation_hours,windspeed_10m_max,"
    "temperature_2m_min,temperature_2m_max,weathercode"
)
HOURLY_FIELDS = "temperature_2m,precipitation,windspeed_10m,weathercode"
TARGET_HOUR = "09:00"


def get_object_coords():
    lat_row = query_one("select value from app_setting where key='object_lat'")
    lon_row = query_one("select value from app_setting where key='object_lon'")
    if not lat_row or not lon_row:
        raise RuntimeError(
            "Координаты объекта не заданы в app_setting (object_lat/object_lon). "
            "Задать один раз, см. задачу про daily_weather."
        )
    return float(lat_row["value"]), float(lon_row["value"])


def get_object_timezone():
    row = query_one("select value from app_setting where key='object_timezone'")
    tz_name = row["value"] if row and row["value"] else "Asia/Vladivostok"
    try:
        ZoneInfo(tz_name)
    except Exception:
        tz_name = "Asia/Vladivostok"
    return tz_name


def fetch_range(lat, lon, start_date, end_date, api, tz_name):
    base = (
        "https://archive-api.open-meteo.com/v1/archive"
        if api == "archive"
        else "https://api.open-meteo.com/v1/forecast"
    )
    url = (
        f"{base}?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily={DAILY_FIELDS}&hourly={HOURLY_FIELDS}"
        f"&timezone={tz_name}&windspeed_unit=ms"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        return None, None, f"{api} API недоступен: {e}"
    if "daily" not in data:
        return None, None, f"{api} API вернул ответ без поля daily: {json.dumps(data)[:300]}"
    if "hourly" not in data:
        return data["daily"], None, f"{api} API вернул ответ без поля hourly: {json.dumps(data)[:300]}"
    return data["daily"], data["hourly"], None


def index_hourly_09(hourly):
    """{"YYYY-MM-DD": index в hourly["time"]} для записей ровно на 09:00
    местного времени (сам API уже отдаёт время в timezone из запроса —
    не UTC, пересчитывать не нужно)."""
    if not hourly:
        return {}
    by_date = {}
    for i, ts in enumerate(hourly["time"]):
        # ts вида "2026-07-01T09:00"
        if ts.endswith("T" + TARGET_HOUR):
            by_date[ts[:10]] = i
    return by_date


def upsert_day(d, precip_mm, precip_hours, wind_ms, tmin, tmax, code,
                temp_09, precip_09, wind_09, code_09,
                source, status, error_note):
    execute(
        """
        insert into daily_weather
            (date, precipitation_mm, precipitation_hours, wind_max_ms,
             temp_min_c, temp_max_c, weathercode,
             temp_09_c, precipitation_09_mm, wind_09_ms, weathercode_09,
             source, status, error_note, fetched_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        on conflict (date) do update set
            precipitation_mm = excluded.precipitation_mm,
            precipitation_hours = excluded.precipitation_hours,
            wind_max_ms = excluded.wind_max_ms,
            temp_min_c = excluded.temp_min_c,
            temp_max_c = excluded.temp_max_c,
            weathercode = excluded.weathercode,
            temp_09_c = excluded.temp_09_c,
            precipitation_09_mm = excluded.precipitation_09_mm,
            wind_09_ms = excluded.wind_09_ms,
            weathercode_09 = excluded.weathercode_09,
            source = excluded.source,
            status = excluded.status,
            error_note = excluded.error_note,
            fetched_at = now()
        """,
        (d, precip_mm, precip_hours, wind_ms, tmin, tmax, code,
         temp_09, precip_09, wind_09, code_09,
         source, status, error_note),
    )


def sync_range(start_date, end_date, api):
    lat, lon = get_object_coords()
    tz_name = get_object_timezone()
    daily, hourly, err = fetch_range(lat, lon, start_date, end_date, api, tz_name)
    source = f"open-meteo-{api}-hourly09"

    if daily is None:
        d = start_date
        n = 0
        while d <= end_date:
            upsert_day(d, None, None, None, None, None, None,
                       None, None, None, None, source, "error", err)
            d += timedelta(days=1)
            n += 1
        print(f"ERROR: {err} — помечено error дней: {n}")
        return 1

    hourly_09_idx = index_hourly_09(hourly)
    n_ok = 0
    n_err = 0
    n_no09 = 0
    for i, iso_day in enumerate(daily["time"]):
        d = date.fromisoformat(iso_day)
        precip = daily["precipitation_sum"][i]
        hours = daily["precipitation_hours"][i]
        wind = daily["windspeed_10m_max"][i]
        tmin = daily["temperature_2m_min"][i]
        tmax = daily["temperature_2m_max"][i]
        code = daily["weathercode"][i]

        h_idx = hourly_09_idx.get(iso_day)
        temp_09 = precip_09 = wind_09 = code_09 = None
        if h_idx is not None:
            temp_09 = hourly["temperature_2m"][h_idx]
            precip_09 = hourly["precipitation"][h_idx]
            wind_09 = hourly["windspeed_10m"][h_idx]
            code_09 = hourly["weathercode"][h_idx]
        else:
            n_no09 += 1

        if precip is None and tmin is None and tmax is None and h_idx is None:
            upsert_day(d, None, None, None, None, None, None,
                       None, None, None, None,
                       source, "error", "API вернул пустые значения на эту дату (ни суточных, ни на 09:00)")
            n_err += 1
        else:
            upsert_day(d, precip, hours, wind, tmin, tmax, code,
                       temp_09, precip_09, wind_09, code_09,
                       source, "ok", None if h_idx is not None else "нет часовых данных на 09:00 для этой даты")
            n_ok += 1
    print(f"OK: {api} {start_date}..{end_date} — записано ok={n_ok}, error={n_err}, "
          f"из ok без замера на 09:00={n_no09}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backfill", "daily"], required=True)
    p.add_argument("--start", help="YYYY-MM-DD, только для --mode backfill")
    p.add_argument("--end", help="YYYY-MM-DD, только для --mode backfill")
    args = p.parse_args()

    if args.mode == "backfill":
        if not args.start or not args.end:
            print("--mode backfill требует --start и --end", file=sys.stderr)
            sys.exit(2)
        sys.exit(sync_range(date.fromisoformat(args.start), date.fromisoformat(args.end), "archive"))
    else:
        tz_name = get_object_timezone()
        today_obj = datetime.now(ZoneInfo(tz_name)).date()
        yesterday = today_obj - timedelta(days=1)
        sys.exit(sync_range(yesterday, today_obj, "forecast"))
