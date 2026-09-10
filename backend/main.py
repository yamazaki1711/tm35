import bisect
import contextvars
import hashlib
import json
import math
import os
import re
import secrets
import urllib.parse
from collections import defaultdict

import psycopg2.errors
from datetime import date as date_cls, datetime as datetime_cls, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from db import query, query_one, execute, run_in_transaction
from rsk_parser import parse_act
from grafik_matching import (
    build_row_index, resolve_group_tokens, resolve_tokens, tokenize_group_name,
)
from analytics import (
    compute_overdue, compute_project_forecast, compute_resource_deficit, DONE_STATUSES,
    compute_work_weight, compute_weighted_progress, compute_evm, compute_ppc,
    compute_required_people, compute_forecast_from_people, compute_forecast_by_pace,
    compute_schedule_position,
)

class NoCacheStaticFiles(StaticFiles):
    """Без Cache-Control браузер живёт на эвристическом кэше неделями и не
    перезапрашивает style.css/js даже после деплоя новой вёрстки — новая
    разметка рендерится со старыми стилями. no-cache вынуждает ревалидацию
    по ETag на каждый заход (304, не полная перекачка), а не отключает
    кэш совсем."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="ТМ-35 Мониторинг")
app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")

# Загруженные PDF предписаний (координатор, 04.09.2026 — п.C1). Тот же
# уровень надёжности хранения, что у остального кода приложения: живёт в
# писуемом слое контейнера, не на отдельном volume (его тут нет ни у чего
# другого) — переживает docker restart, не переживёт пересоздание
# контейнера без повторного docker cp, как и main.py/templates.
UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "prescriptions")
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount("/uploads/prescriptions", StaticFiles(directory=UPLOADS_DIR), name="prescription_uploads")

# Загруженные акты РСК — между "предпросмотром" и "подтверждением" формы
# «Загрузка акта проверки» (см. секцию РСК ниже): файл сохраняется под
# токеном при предпросмотре, подтверждение читает его повторно и удаляет.
RSK_UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "rsk_acts")
os.makedirs(RSK_UPLOADS_DIR, exist_ok=True)

templates = Jinja2Templates(directory="templates")

# =======================================================================
# Учётные записи и разграничение доступа (документ «Ответственные по
# разделам», решение координатора 29.08.2026). Просмотр сайта —
# публичный (basic-auth снимается на nginx), любое изменение данных —
# только под учётной записью. Логин/пароль привязаны к app_user (не
# заводим второй список фамилий — тот же принцип, что уже нарушался
# один раз с downtime_cause/REASON_CODES, координатор просил не
# повторять).
# =======================================================================

PBKDF2_ITERATIONS = 200_000
SESSION_COOKIE = "tm35_session"
SESSION_TTL_DAYS = 30

CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


def _clean_none_for_display(value):
    """Рекурсивно заменяет None на "—" в словаре/списке — только для
    показа (raw_payload на /quality), не трогает то, что хранится в БД."""
    if isinstance(value, dict):
        return {k: _clean_none_for_display(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_none_for_display(v) for v in value]
    return "—" if value is None else value


def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16)
    pw_norm = password.strip().lower()  # регистр не важен — пароль и так только из строчных букв
    dk = hashlib.pbkdf2_hmac("sha256", pw_norm.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return dk.hex(), salt.hex()


def verify_password(password, hash_hex, salt_hex):
    if not hash_hex or not salt_hex:
        return False
    dk_hex, _ = hash_password(password, bytes.fromhex(salt_hex))
    return secrets.compare_digest(dk_hex, hash_hex)


def create_session(user_id, ip, user_agent):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    execute(
        "insert into user_session (token_hash, user_id, expires_at, ip, user_agent) "
        "values (%s, %s, now() + make_interval(days => %s), %s, %s)",
        (token_hash, user_id, SESSION_TTL_DAYS, ip, user_agent),
    )
    return token


def get_user_by_session(token):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = query_one(
        "select u.id, u.full_name, u.login, u.role "
        "from user_session s join app_user u on u.id = s.user_id "
        "where s.token_hash=%s and s.expires_at > now() and u.is_active",
        (token_hash,),
    )
    return row


def is_admin(user):
    if not user:
        return False
    perms = query("select permission from user_permission where user_id=%s", (user["id"],))
    return any(p["permission"] == "admin" for p in perms)


def user_permissions(user):
    if not user:
        return set()
    rows = query("select permission from user_permission where user_id=%s", (user["id"],))
    return {r["permission"] for r in rows}


def has_permission(user, permission):
    if not user:
        return False
    perms = user_permissions(user)
    if "admin" in perms or permission in perms:
        return True
    # Права по веткам, 30.08.2026 (решение координатора): вместо
    # поимённых допусков на каждую вкладку ИД — две крупные группы,
    # "zone:id" (вся ветка ИД без исключений) и "zone:smr" (вся ветка
    # СМР). Поимённое разграничение по разделам вернётся позже —
    # намеренно НЕ удаляю конкретные id_tab:xxx/changes:submit/
    # prescriptions:submit ни из кода, ни у людей, которым они ещё
    # нужны индивидуально: зонное право — запасной путь ЗДЕСЬ, в одном
    # месте, а не отдельная ветка в каждом вызывающем коде. Когда
    # понадобится точечно сузить кого-то из группы — снять "zone:id" и
    # выдать конкретные id_tab:xxx, код трогать не придётся.
    if "zone:id" in perms and (permission.startswith("id_tab:") or permission in ("changes:submit", "prescriptions:submit", "id-folders:submit", "rsk:submit")):
        return True
    if "zone:smr" in perms and permission == "smr:write":
        return True
    return False


# Маршруты, меняющие данные (POST/PUT/PATCH/DELETE), кроме самой формы
# входа — требуют действующей сессии. Список сверен со всеми @app.post
# в этом файле перед снятием basic-auth (см. docs/AUTH_2026-08-29.md).
AUTH_EXEMPT_PATHS = {"/login"}


_current_user_var = contextvars.ContextVar("tm35_current_user", default=None)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = request.cookies.get(SESSION_COOKIE)
        user = get_user_by_session(token) if token else None
        request.state.user = user
        _current_user_var.set(user)

        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path not in AUTH_EXEMPT_PATHS:
            if not user:
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"ok": False, "error": "Требуется вход в систему."}, status_code=401)
                return RedirectResponse(
                    url=f"/login?next={urllib.parse.quote(request.url.path)}", status_code=303
                )

        response = await call_next(request)
        return response


app.add_middleware(AuthMiddleware)


@app.get("/login")
def login_page(request: Request, next: str = "/", err: str = ""):
    if request.state.user:
        return RedirectResponse(url=next or "/", status_code=303)
    return render(request, "login.html", "login", next=next, err=err)


@app.post("/login")
def login_post(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")
    login_norm = login.strip().lower()

    if CYRILLIC_RE.search(login) or CYRILLIC_RE.search(password):
        execute(
            "insert into login_log (login_attempted, success, reason, ip, user_agent) values (%s, false, %s, %s, %s)",
            (login_norm, "кириллица в вводе — похоже, не та раскладка", ip, user_agent),
        )
        return RedirectResponse(
            url=f"/login?next={urllib.parse.quote(next)}&err=layout", status_code=303
        )

    user = query_one(
        "select id, full_name, login, password_hash, password_salt from app_user "
        "where lower(login)=%s and is_active", (login_norm,)
    )
    ok = user and verify_password(password, user["password_hash"], user["password_salt"])
    execute(
        "insert into login_log (login_attempted, user_id, success, ip, user_agent) values (%s, %s, %s, %s, %s)",
        (login_norm, user["id"] if user else None, bool(ok), ip, user_agent),
    )
    if not ok:
        return RedirectResponse(url=f"/login?next={urllib.parse.quote(next)}&err=badpass", status_code=303)

    token = create_session(user["id"], ip, user_agent)
    resp = RedirectResponse(url=next or "/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", secure=True,
    )
    return resp


@app.post("/logout")
def logout_post(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        execute("delete from user_session where token_hash=%s", (token_hash,))
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# Русские подписи вместо кодов схемы БД — жалоба координатора: "почему так
# много слов на английском". Коды остаются в БД (для запросов/аналитики),
# в интерфейсе — только перевод, через Jinja-фильтры ниже.
RU_STATUS = {
    "not_started": "не начата", "in_progress": "в работе", "suspended": "приостановлена",
    "limited": "ограничена", "done_physically": "выполнена физически", "submitted": "предъявлена",
    "accepted": "принята", "closed": "закрыта", "cancelled": "отменена",
}
RU_SOURCE = {"main": "основные", "aux": "вспомогательные"}
RU_EXECUTOR = {"own_forces": "свои силы", "subcontract": "субподряд"}
RU_BLOCKER_TYPE = {
    "material": "материал", "delivery": "поставка", "equipment": "техника", "fuel": "ГСМ",
    "weather": "погода", "front": "фронт работ", "design_decision": "проектное решение",
    "subcontract": "субподряд", "contract": "договор", "payment": "оплата",
    "acceptance": "приёмка", "sequence": "очерёдность", "aux_reallocation": "переброска на вспом. работы",
    "id_docs": "документы ИД",
}
RU_BLOCKER_STATUS = {"active": "активно", "resolved": "снято"}
RU_DATA_QUALITY = {"ok": "ок", "needs_review": "проверить"}
RU_MATERIAL_STATUS = {
    "requested": "заявка", "ordered": "заказан", "paid": "оплачен",
    "in_transit": "в пути", "on_site": "на объекте", "missing": "отсутствует",
}
RU_DP_SOURCE = {"excel_import": "из Excel", "web_form": "веб-форма"}
RU_CONFIDENCE = {"high": "высокая", "medium": "средняя", "low": "низкая", "none": "нет данных"}
RU_BASELINE_SOURCE = {
    "matrix_schedule": "календарная матрица графика", "text_month_only": "текст, только месяц",
    "no_data": "нет данных", "web_form": "веб-форма",
}
REASON_CODES = [
    ("WEATHER_RAIN", "Дождь"),
    ("WEATHER_WIND", "Ветер"),
    ("WEATHER_TEMP", "Температурные ограничения"),
    ("FUEL_MISSING", "Отсутствие ГСМ"),
    ("MATERIAL_MISSING", "Отсутствие материала"),
    ("MATERIAL_DELIVERY", "Задержка поставки материала"),
    ("EQUIPMENT_MISSING", "Отсутствие техники"),
    ("EQUIPMENT_BROKEN", "Поломка техники"),
    ("FRONT_MISSING", "Отсутствие фронта работ"),
    ("DESIGN_MISSING", "Отсутствие проектного решения"),
    ("SUBCONTRACT_MISSING", "Субподрядчик не включился"),
    ("CONTRACT_NOT_SIGNED", "Договор не подписан"),
    ("PAYMENT_MISSING", "Отсутствие оплаты"),
    ("AUX_REALLOCATION", "Переброска на вспомогательные работы"),
    ("ACCEPTANCE_WAIT", "Ожидание приёмки/предъявления"),
    ("SEQUENCE_WAIT", "Ожидание предыдущей работы"),
    ("PLANNING_ERROR", "Ошибка планирования"),
    ("OTHER", "Иное (указать в комментарии)"),
]
REASON_CODE_SET = {c for c, _ in REASON_CODES}
RU_REASON_CODE = dict(REASON_CODES)
# Подготовка пилота, 30.08.2026 — /journal показывал сырые имена таблиц
# БД в столбце "объект" (daily_progress и т.п.), тот же класс нарушения,
# что правило проекта запрещает для остального интерфейса.
RU_ENTITY_TYPE = {
    "daily_progress": "Факт СМР", "id_form_entry": "Запись ИД", "work": "Работа",
    "app_setting": "Настройка объекта", "baseline_schedule": "Плановый срок",
    "app_user": "Учётная запись", "change": "ИЗМ", "prescription": "Предписание",
    "blocker": "Стоп-фактор", "id_form_block": "Блокировка ИЗМ (ИД)",
    "current_schedule": "Сдвиг сроков (график)",
}
templates.env.filters["ru_status"] = lambda v: RU_STATUS.get(v, v)
templates.env.filters["ru_source"] = lambda v: RU_SOURCE.get(v, v)
templates.env.filters["ru_executor"] = lambda v: RU_EXECUTOR.get(v, v)
templates.env.filters["ru_blocker_type"] = lambda v: RU_BLOCKER_TYPE.get(v, v)
templates.env.filters["ru_blocker_status"] = lambda v: RU_BLOCKER_STATUS.get(v, v)
templates.env.filters["ru_material_status"] = lambda v: RU_MATERIAL_STATUS.get(v, v)
templates.env.filters["ru_dp_source"] = lambda v: RU_DP_SOURCE.get(v, v)
templates.env.filters["ru_confidence"] = lambda v: RU_CONFIDENCE.get(v, v)
templates.env.filters["ru_baseline_source"] = lambda v: RU_BASELINE_SOURCE.get(v, v)
templates.env.filters["ru_reason_code"] = lambda v: RU_REASON_CODE.get(v, v)
templates.env.filters["ru_entity_type"] = lambda v: RU_ENTITY_TYPE.get(v, v)
# Подготовка пилота, 30.08.2026 — /settings/users показывал внутренние
# коды разрешений как есть (zone:id, zone:smr, id_tab:xxx). Список из
# нескольких через запятую (string_agg в запросе) — переводим каждый
# токен отдельно, id_tab:xxx оставляем узнаваемым (код вкладки виден,
# это НЕ то же самое, что общий перевод остальных кодов, вкладок много
# и заводить на каждую отдельную строку сейчас избыточно).
RU_PERMISSION = {"admin": "Координатор (все права)", "zone:id": "Группа ИД (вся ветка)",
                  "zone:smr": "Группа СМР (вся ветка)"}


def _ru_permission_one(p):
    p = p.strip()
    if p in RU_PERMISSION:
        return RU_PERMISSION[p]
    if p.startswith("id_tab:"):
        return "вкладка ИД: " + p.split(":", 1)[1]
    if p == "changes:submit":
        return "форма ИЗМ"
    if p == "prescriptions:submit":
        return "форма предписаний"
    return p


def _ru_permission_list(v):
    if not v:
        return v
    return ", ".join(_ru_permission_one(p) for p in v.split(","))


templates.env.filters["ru_permission_list"] = _ru_permission_list

# Импортёр (import/load_to_postgres.py) хранит служебный префикс в
# blocker.description для идемпотентной перезагрузки (см. DAY_BLOCKER_MARKER
# там же) — пользователю он виден быть не должен, только сам текст причины.
# "WORK:" — не наш префикс, а часть исходного текста комментария в самом
# Excel-файле (см. .claude/skills/tm35-excel/SKILL.md, "источник стоп-
# факторов и WORK:-пометок") — тоже служебная пометка, тоже не для показа.
_DAY_BLOCKER_MARKER = "[день-уровень, Excel]: "
_WORK_PREFIX_RE = re.compile(r"^WORK:\s*")


def _strip_source_marker(v):
    if not v:
        return v
    if v.startswith(_DAY_BLOCKER_MARKER):
        v = v[len(_DAY_BLOCKER_MARKER):]
    v = _WORK_PREFIX_RE.sub("", v)
    return v


templates.env.filters["strip_source_marker"] = _strip_source_marker


def _dmy(value):
    """Единый формат отображения дат по всему интерфейсу — ДД.ММ.ГГГГ.
    Хранение остаётся ISO (в БД и в скрытых полях форм), фильтр только
    для вывода. Принимает date/datetime, ISO-строку или пусто.

    timestamptz-значения (есть .hour) пересчитываются в часовой пояс
    объекта ПЕРЕД форматированием (решение координатора 29.08.2026) —
    иначе календарная дата у записи, сделанной поздним вечером по
    Хабаровску, могла бы печататься по UTC-дате (более ранней). Голые
    `date`-колонки не трогаем — они уже трактуются как хабаровские сутки
    без пересчёта."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = date_cls.fromisoformat(value[:10])
        except ValueError:
            return value
    if hasattr(value, "hour"):
        value = to_object_tz(value)
    return value.strftime("%d.%m.%Y")


templates.env.filters["dmy"] = _dmy


def _ru_money(v):
    """Единственное место форматирования денег в интерфейсе — запятая
    вместо точки у копеек, неразрывный пробел (не обычный) между
    разрядами, чтобы крупная сумма не переносилась посередине на узкой
    колонке. Раньше каждый шаблон делал `'{:,.2f}'.format(x)|replace(',',
    ' ')` у себя — точка у копеек и обычный (разрывной) пробел были
    скопированы в 5 файлов одинаково неверно."""
    if v is None:
        return "—"
    s = "{:,.2f}".format(float(v))
    int_part, dec_part = s.split(".")
    return int_part.replace(",", " ") + "," + dec_part


templates.env.filters["ru_money"] = _ru_money

# "Последняя запись побеждает" между excel_import и web_form за один
# (дата, работа) — обе строки физически остаются в daily_progress
# (unique включает source), эта CTE выбирает победителя для отображения.
#
# Правка 31.08.2026 (дефект №4, KNOWN_ISSUES.md): раньше побеждала строка
# ЦЕЛИКОМ — если веб-форма в тот же день писала только факт (planned_crew
# всегда NULL у неё), она как "последняя по updated_at" затирала план,
# внесённый Excel-строкой того же дня, во всей сумме по объекту. План
# теперь ищется НЕЗАВИСИМО от остальных полей: latest_plan берёт
# последнюю строку, где planned_crew реально задан (неважно, какой
# источник), latest_dp подставляет его поверх обычного full-row-winner.
# Факт и все прочие поля (actual_crew, fact_pct, comment, reason_code,
# source, updated_at) — по-старому, последняя строка целиком, это не
# было ошибкой и не трогается. Проверено на 28.08.2026: план 6 -> 34,
# факт не изменился (9); построчных потерь/дублей не внесено (общее
# число строк latest_dp до/после правки совпадает).
LATEST_DP_CTE = """
with latest_dp_raw as (
    select distinct on (date, work_id) *
    from daily_progress
    order by date, work_id, updated_at desc
),
latest_plan as (
    select distinct on (date, work_id) date, work_id, planned_crew, planned_crew_raw, planned_hours
    from daily_progress
    where planned_crew is not null
    order by date, work_id, updated_at desc
),
latest_dp as (
    select
        r.id, r.date, r.work_id,
        coalesce(lp.planned_crew, r.planned_crew) as planned_crew,
        r.actual_crew,
        coalesce(lp.planned_hours, r.planned_hours) as planned_hours,
        r.actual_hours, r.stop_hours, r.done_volume,
        r.fact_pct, r.fact_pct_raw, r.status, r.reason_code, r.comment, r.source,
        r.data_quality_flag, r.data_quality_note, r.created_by, r.created_at, r.updated_at,
        coalesce(lp.planned_crew_raw, r.planned_crew_raw) as planned_crew_raw,
        r.actual_crew_raw
    from latest_dp_raw r
    left join latest_plan lp on lp.date = r.date and lp.work_id = r.work_id
)
"""

WEB_FORM_USER_NAME = "Веб-форма ТМ-35 (общий вход tm-35)"

# Коды погоды WMO (daily_weather.weathercode, отдаёт Open-Meteo) — для
# человекочитаемой автоподстановки в поле "Погода" на /report. Только для
# отображения, на бизнес-логику не влияет.
WMO_WEATHER_RU = {
    0: "ясно", 1: "малооблачно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман",
    51: "морось", 53: "морось", 55: "морось",
    56: "ледяная морось", 57: "ледяная морось",
    61: "дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь",
    71: "снег", 73: "снег", 75: "сильный снег",
    77: "снежная крупа",
    80: "ливень", 81: "ливень", 82: "сильный ливень",
    85: "снежный ливень", 86: "снежный ливень",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}


def format_auto_weather(row):
    """daily_weather row -> человекочитаемая строка для поля "Погода".
    Только предложение автозаполнения — ничего не сохраняет и не решает,
    показывать её или нет (это делает вызывающий код).

    ВЕРСИЯ 29.08.2026 (задание координатора): раньше это был суточный
    агрегат (мин/макс температуры, макс. ветра за день) — заменено на
    замер РОВНО на 09:00 утра по времени объекта (начало смены), той же
    точки, что теперь качает weather_sync.py в колонки *_09.
    Суточные колонки в daily_weather никуда не делись (не удалялись по
    правилу проекта), просто эта функция их больше не читает — если
    понадобятся, у них есть все прежние данные.

    Требование задания: если для даты нет замера на 09:00 (temp_09_c
    is null), НЕ подставлять суточный агрегат молча — возвращаем None,
    вызывающий код оставляет поле пустым."""
    if row["temp_09_c"] is None and row["precipitation_09_mm"] is None and row["wind_09_ms"] is None:
        return None

    precip = row["precipitation_09_mm"]
    code = row["weathercode_09"]
    desc = WMO_WEATHER_RU.get(code, "осадки" if precip and precip > 0 else "погода")
    if precip is not None and precip > 0:
        desc = f"{desc}, {float(precip):.1f} мм"

    def _signed(v):
        v = float(v)
        return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"

    parts = [desc]
    if row["wind_09_ms"] is not None:
        parts.append(f"ветер {float(row['wind_09_ms']):.1f} м/с")
    if row["temp_09_c"] is not None:
        parts.append(f"{_signed(row['temp_09_c'])}°C")
    parts.append("на 09:00")
    return ", ".join(parts)


def render(request, template, active, **ctx):
    # object_today_iso — единая точка входа "сегодня по объекту" для
    # ЛЮБОГО клиентского JS (datepicker.js и т.п. использовали свой
    # new Date(), т.е. "сегодня" по часовому поясу БРАУЗЕРА зрителя, не
    # объекта — координатор мог смотреть дашборд из Москвы и видеть не
    # тот день в календарике). Кладётся здесь один раз, а не в каждом
    # route, чтобы точно не забыть на новой странице.
    ctx.setdefault("object_today_iso", object_today().isoformat())
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    resp = templates.TemplateResponse(request, template, {"active": active, **ctx})
    # Без этого браузер иногда отдаёт со страницы кэшированную копию после
    # редиректа с сохранения формы (координатор, 04.09.2026: сумма папки
    # сохранилась в БД, но карточка на экране показывала старое значение,
    # пока не обновишь руками) — все страницы всегда генерируются заново.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------
# «Сегодня» по календарю ОБЪЕКТА (Хабаровск, UTC+10), не по UTC сервера
# (28.08.2026, требования Якименко А.И. — регламент «до 11:00 заполняем
# за вчера»). Контейнер живёт в UTC без TZ: с 00:00 до 10:00 по объекту
# серверное date.today() отстаёт на сутки — окно ровно то самое, когда
# инженер ещё вносит вчерашний факт. Часовой пояс — не константа в коде,
# а app_setting.object_timezone (значение по задаче — 'Asia/Vladivostok',
# тот же офсет UTC+10, без перехода на летнее время), чтобы не
# перевыпускать деплой, если объект сменится.
_OBJECT_TZ_CACHE = {"tz": None, "raw": None}


def object_timezone():
    row = query_one("select value from app_setting where key='object_timezone'")
    raw = row["value"] if row and row["value"] else "Asia/Vladivostok"
    if _OBJECT_TZ_CACHE["raw"] != raw:
        try:
            _OBJECT_TZ_CACHE["tz"] = ZoneInfo(raw)
        except Exception:
            _OBJECT_TZ_CACHE["tz"] = ZoneInfo("Asia/Vladivostok")
        _OBJECT_TZ_CACHE["raw"] = raw
    return _OBJECT_TZ_CACHE["tz"]


def object_today():
    return datetime_cls.now(object_timezone()).date()


def object_yesterday():
    return object_today() - timedelta(days=1)


def to_object_tz(dt):
    """Решение координатора 29.08.2026 ("ВЕСЬ проект живёт по хабаровскому
    времени"): хранение (timestamptz) остаётся в UTC — меняется только
    ПОКАЗ. psycopg2 отдаёт timestamptz как datetime с tzinfo=UTC — прямой
    .isoformat()/.strftime() на таком значении печатает UTC-цифры как
    есть (не ошибка Python, ошибка в том, что мы эти цифры показывали
    пользователю без пересчёта). Эта функция — единая точка пересчёта
    перед ЛЮБЫМ показом времени (не даты — календарные date-колонки уже
    трактуются как хабаровские сутки без пересчёта, см. object_today())."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(object_timezone())


def ensure_web_form_user():
    row = query_one("select id from app_user where full_name=%s", (WEB_FORM_USER_NAME,))
    if row:
        return row["id"]
    execute(
        "insert into app_user (full_name, role) values (%s, 'executor') "
        "on conflict do nothing",
        (WEB_FORM_USER_NAME,),
    )
    row = query_one("select id from app_user where full_name=%s", (WEB_FORM_USER_NAME,))
    return row["id"] if row else None


def current_user_id_or_web_form():
    """Учётные записи, 29.08.2026: audit_log должен привязываться к
    реальному вошедшему человеку, а не к общему "Веб-форма ТМ-35"
    пользователю. AuthMiddleware уже гарантирует вход для любого
    POST/PUT/PATCH/DELETE (кроме /login) — к моменту вызова этой функции
    внутри обработчика пользователь почти всегда есть; запасной вариант
    (ensure_web_form_user) — на случай вызова из кода, который сам не
    защищён мидлварью (например, скрипты импорта, дергающие эти функции
    напрямую в обход HTTP)."""
    user = _current_user_var.get()
    if user:
        return user["id"]
    return ensure_web_form_user()


def _work_status_expr(alias="w"):
    """Единственное определение "статуса работы" в проекте — вычисляется
    из fact_pct на каждый запрос, не читает столбец work.status: тот
    заполняется один раз при импорте и с тех пор не обновляется, хотя
    fact_pct живой (обновляется формой ввода факта, main.py). Найдено
    координатором 08.09.2026: 100 из 169 работ показывались "не начата"
    неделями, включая работы со 100% факта, потому что дашборд читал
    именно этот замороженный столбец. Порог 0%/100% и трактовка
    fact_pct IS NULL как "нет факта" — то же самое, что уже использует
    compute_weighted_progress (analytics.py) для взвешенного процента:
    один и тот же критерий "есть ли факт", а не отдельное правило.
    Используется везде, где раньше читали w.status для логики (не для
    чисто информационного отображения/фильтра на /works — та страница
    вне периметра этой задачи, см. отчёт)."""
    col = f"{alias}.fact_pct" if alias else "fact_pct"
    return (
        f"(case when {col} is null or {col} = 0 then 'not_started' "
        f"when {col} >= 100 then 'done_physically' "
        f"else 'in_progress' end)"
    )


CHANGE_RESOLVED_STATUSES = ('SOLUTION_RECEIVED', 'IKS_ORDER', 'INCLUDED_IN_RD', 'ARCHIVED')


def _change_overdue_expr(alias=""):
    """Единственное определение просрочки ИЗМ — вычисляется от
    CURRENT_DATE на каждый запрос, не читает столбец change.overdue_days:
    тот пересчитывался только в момент правки записи или смены статуса
    (main.py, api_change_update_status/changes_post), между событиями не
    менялся — тот же диагноз, что был у work.status (координатор,
    08.09.2026, аудит целостности). NULL — если ответ уже получен, ИЗМ
    в терминальном статусе, или срок ответа ещё не наступил; иначе —
    число дней просрочки."""
    p = f"{alias}." if alias else ""
    resolved = ",".join(f"'{s}'" for s in CHANGE_RESOLVED_STATUSES)
    return (
        f"(case when {p}actual_response_date is not null "
        f"or {p}status in ({resolved}) "
        f"or {p}planned_response_date is null "
        f"or {p}planned_response_date >= current_date "
        f"then null else (current_date - {p}planned_response_date) end)"
    )


def get_remaining_effort():
    """
    Остаток трудоёмкости (чел-дни) ТОЛЬКО по работам с реальной
    трудоёмкостью из календарной матрицы — не выдумываем для остальных.
    Находка координатора: прежняя "требуемая численность" (сумма "Кол-во
    чел." по всем активным работам, без учёта прогресса) давала 272 при
    12 фактических — бессмысленно завышенное число. Правильная формула —
    остаток / рабочие дни до срока (backend/analytics.py, с тестами).
    """
    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as t "
        "from daily_progress where source='excel_import' and planned_crew is not null "
        "group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: float(r["t"]) for r in trudoemkost_rows}

    works = query(f"select id, fact_pct, {_work_status_expr(None)} as status from work")
    remaining = 0.0
    known_count = 0
    excluded_count = 0
    for w in works:
        if w["status"] in DONE_STATUSES:
            continue
        t = trudoemkost_by_work.get(w["id"])
        if t is None:
            excluded_count += 1
            continue
        known_count += 1
        pct = float(w["fact_pct"]) if w["fact_pct"] is not None else 0.0
        remaining += t * (1 - pct / 100)
    return remaining, known_count, excluded_count


@app.get("/api/calculator")
def api_calculator(target_date: str = "", available_people: str = ""):
    remaining, known_count, excluded_count = get_remaining_effort()
    today = object_today()
    calc_start = schedule_calc_start()
    result = {
        "remaining_effort_days": round(remaining, 1),
        "known_work_count": known_count,
        "excluded_work_count": excluded_count,
        "today": today.isoformat(),
        "calc_start": calc_start.isoformat(),
    }
    if target_date.strip():
        try:
            td = date_cls.fromisoformat(target_date.strip())
        except ValueError:
            return JSONResponse({"error": "Некорректная дата"}, status_code=400)
        req, wd = compute_required_people(remaining, calc_start, td)
        result.update({"mode": "date_to_people", "target_date": td.isoformat(), "working_days": wd, "required_people": req})
    elif available_people.strip():
        try:
            people = int(available_people.strip())
        except ValueError:
            return JSONResponse({"error": "Некорректное число людей"}, status_code=400)
        needed_days, forecast = compute_forecast_from_people(remaining, people, calc_start)
        result.update({
            "mode": "people_to_date", "available_people": people,
            "working_days_needed": needed_days, "forecast_date": forecast.isoformat() if forecast else None,
        })
    return result


@app.get("/calculator")
def calculator_page(request: Request):
    """
    СМР-задание 29.08.2026 (п.3, "Главное" — Якименко А.И.): интерфейс
    поверх уже готового /api/calculator (get_remaining_effort +
    compute_required_people/compute_forecast_from_people из analytics.py,
    формулы не переписывались) — раньше расчёт существовал только как
    JSON-эндпоинт, ни одна страница на него не ссылалась.
    """
    remaining, known_count, excluded_count = get_remaining_effort()
    directive_deadline = get_directive_deadline()
    directive_start = get_directive_start()
    return render(
        request, "calculator.html", "calculator",
        remaining_effort_days=round(remaining, 1),
        known_work_count=known_count, excluded_work_count=excluded_count,
        directive_deadline=directive_deadline.isoformat() if directive_deadline else None,
        directive_start=directive_start.isoformat() if directive_start else None,
        today=object_today().isoformat(),
    )


def get_evm_data():
    """
    EVM/PPC-слой (доменный разбор координатора v2.0, "Измерительный слой").
    Трудоёмкость = Σ planned_crew из календарной матрицы Excel — подтверждено
    сверкой с "Кол-во чел." на реальных данных (docs/REENGINEERING_LOG.md).
    Формулы и тесты — backend/analytics.py.
    """
    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]
    if not last_actual_date:
        return {"available": False}

    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as trudoemkost "
        "from daily_progress where source='excel_import' and planned_crew is not null "
        "group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: r["trudoemkost"] for r in trudoemkost_rows}

    works = query(
        """
        select w.id, w.code, w.fact_pct, bs.plan_crew as baseline_crew
        from work w
        left join baseline_schedule bs on bs.work_id = w.id
        """
    )
    weight_source_counts = {"trudoemkost": 0, "baseline_crew": 0, "flat": 0}
    weighted_input = []
    for w in works:
        weight, src = compute_work_weight(trudoemkost_by_work.get(w["id"]), w["baseline_crew"])
        weight_source_counts[src] += 1
        weighted_input.append({"code": w["code"], "weight": weight, "fact_pct": w["fact_pct"]})

    weighted_pct, total_weight, unknown_pct_count = compute_weighted_progress(weighted_input)

    bcws = query_one(
        LATEST_DP_CTE + "select sum(planned_crew) as v from latest_dp where date <= %s",
        (last_actual_date,),
    )["v"] or 0
    acwp = query_one(
        LATEST_DP_CTE + "select sum(actual_crew) as v from latest_dp where date <= %s",
        (last_actual_date,),
    )["v"] or 0
    total_trudoemkost = sum(trudoemkost_by_work.values()) if trudoemkost_by_work else 0
    bcwp = (weighted_pct or 0) / 100 * total_weight if total_weight else 0

    spi, cpi = compute_evm(bcws, acwp, bcwp)

    trailing_start = last_actual_date - timedelta(days=13)
    ppc_rows = query(
        LATEST_DP_CTE + "select planned_crew, actual_crew from latest_dp where date between %s and %s",
        (trailing_start, last_actual_date),
    )
    ppc_pct, ppc_promised, ppc_met = compute_ppc(ppc_rows)

    return {
        "available": True,
        "last_actual_date": last_actual_date,
        "trailing_start": trailing_start,
        "weighted_pct": weighted_pct,
        "total_weight": total_weight,
        "unknown_pct_count": unknown_pct_count,
        "weight_source_counts": weight_source_counts,
        "works_total": len(works),
        "bcws": bcws,
        "acwp": acwp,
        "bcwp": round(bcwp, 1),
        "total_trudoemkost": total_trudoemkost,
        "spi": spi,
        "cpi": cpi,
        "ppc_pct": ppc_pct,
        "ppc_promised": ppc_promised,
        "ppc_met": ppc_met,
    }


def get_criticality_data():
    """
    Общая для /, /critical, /gantt (api-gantt-metrics) и /data — критичность
    отставания, прогноз завершения, ресурсный дефицит. Формулы и обоснование —
    backend/analytics.py и docs/GAP_ANALYSIS.md (Цикл 1). Использует ТОЛЬКО
    данные из БД — ничего не придумывает; если временного baseline нет для
    работы, она просто не попадает в расчёт (не считается ни просроченной,
    ни в срок).

    Единственный источник "даты завершения" в проекте (докс координатора
    08.09.2026, docs/FORECAST_UNIFICATION_2026-09-08.md) — раньше "Обзор"
    (эта функция) и "/status" (get_scurve_data) считали дату завершения
    двумя разными формулами независимо и расходились (28.11→08.12 здесь
    против 07.01.2027 там). Проверка "нужная численность против реально
    достигнутой" показала: по факту темпа последних 14 дней (~20 чел/день)
    проект не успевает даже к дате "baseline + текущая просрочка" (нужно
    ~27 чел/день) — расчёт по фактическому темпу (compute_forecast_by_pace)
    честнее, он же и стал единственным "forecast_date". Старая формула
    (baseline + средняя просрочка уже просроченных работ) отставлена под
    именем baseline_lag_forecast_date — она недооценивает риск структурно
    (реагирует только на уже просроченные работы, не на общий темп) и
    сохранена лишь для истории графика тренда (forecast_snapshot,
    method='baseline_lag'), в интерфейсе больше не показывается как "прогноз".
    """
    today = object_today()

    works_with_baseline = query(
        f"""
        select w.code, w.name, {_work_status_expr()} as status, bs.plan_finish
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_finish is not null and bs.confidence in ('high', 'medium')
        """
    )
    overdue = compute_overdue(works_with_baseline, today)

    active_finishes = [
        w["plan_finish"] for w in works_with_baseline
        if w["status"] not in DONE_STATUSES
    ]
    overdue_lags = [w["lag_days"] for w in overdue]
    baseline_lag_forecast_date, avg_lag, baseline_date = compute_project_forecast(active_finishes, overdue_lags)

    # Канонический прогноз — по фактическому темпу (см. докстринг выше).
    remaining, known_work_count, excluded_work_count = get_remaining_effort()
    recent_rows = query(
        LATEST_DP_CTE + """
        select date, sum(actual_crew) as v from latest_dp
        where actual_crew is not null
        group by date order by date desc limit 14
        """
    )
    recent_actuals = [float(r["v"] or 0) for r in recent_rows]
    forecast_date, avg_pace, pace_working_days_needed = compute_forecast_by_pace(remaining, recent_actuals, today)

    # Запланировано И фактически вышло на ОДИН И ТОТ ЖЕ день (last_actual_date) —
    # раньше required_crew считался как сумма "Кол-во чел." по ВСЕМ активным
    # работам (272 при 12 фактических — бессмысленное число, не про этот день
    # и не про требуемую численность вообще, см. docs/REENGINEERING_LOG.md).
    # Требуемая численность под директивный срок — отдельно, /api/calculator.
    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]
    required_crew = actual_crew = None
    if last_actual_date:
        day_totals = query_one(
            LATEST_DP_CTE + "select sum(planned_crew) as p, sum(actual_crew) as a from latest_dp where date=%s",
            (last_actual_date,),
        )
        required_crew = day_totals["p"]
        actual_crew = day_totals["a"]

    deficit, surplus, coverage_pct = compute_resource_deficit(required_crew, actual_crew)

    # "Прошло срока" — задание координатора 29.08.2026: раньше считалось
    # от минимального планового начала в baseline_schedule (01.07), что
    # противоречит окну отображения графиков рядом (01.08-28.11) — плитка
    # и график рядом с ней показывали бы разный "старт". Переведено на
    # начало окна отображения (get_display_window()), не на дату из
    # данных — это то же значение, что ограничивает ось /gantt и /status.
    project_start, _window_end = get_display_window()
    elapsed_days = total_days = elapsed_pct = None
    if project_start and baseline_date:
        elapsed_days, total_days, elapsed_pct = compute_schedule_position(
            project_start, baseline_date, today
        )

    return {
        "today": today,
        "overdue": overdue,
        "overdue_count": len(overdue),
        "works_with_baseline_count": len(works_with_baseline),
        "forecast_date": forecast_date,
        "avg_pace": avg_pace,
        "pace_working_days_needed": pace_working_days_needed,
        "remaining_effort_days": remaining,
        "known_work_count": known_work_count,
        "excluded_work_count": excluded_work_count,
        "baseline_lag_forecast_date": baseline_lag_forecast_date,
        "avg_lag": avg_lag,
        "baseline_date": baseline_date,
        "required_crew": required_crew,
        "actual_crew": actual_crew,
        "deficit": deficit,
        "surplus": surplus,
        "coverage_pct": coverage_pct,
        "last_actual_date": last_actual_date,
        "project_start": project_start,
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "elapsed_pct": elapsed_pct,
    }


def get_app_setting(key, default=None):
    row = query_one("select value from app_setting where key=%s", (key,))
    return row["value"] if row else default


def get_directive_deadline():
    v = get_app_setting("directive_deadline")
    if not v:
        return None
    try:
        return date_cls.fromisoformat(v)
    except ValueError:
        return None


def get_directive_start():
    v = get_app_setting("directive_start")
    if not v:
        return None
    try:
        return date_cls.fromisoformat(v)
    except ValueError:
        return None


def schedule_calc_start():
    """
    Точка отсчёта для расчётов "сколько людей нужно до директивного
    срока" (не для прогноза по факту — там анкер всегда object_today(),
    люди реально уже работают). Задание координатора 29.08.2026,
    "Главное!!!": график по контракту начинается 01.09.2026 — если
    считать с текущей даты (например, 28.08, до старта графика), в число
    рабочих дней ложно попадают дни ДО начала графика, требуемая
    численность занижается. object_today(), если директивный старт ещё
    не задан или уже наступил/прошёл (иначе более позднее из двух).
    """
    start = get_directive_start()
    today = object_today()
    if start and start > today:
        return start
    return today


# Окно отображения графиков/диаграмм (решение координатора 29.08.2026):
# 01.08.2026-28.11.2026 — только про то, что ПОКАЗЫВАЕТСЯ на временнЫх
# осях (/gantt, S-кривая на /status, "Прошло срока"), не про то, что
# хранится или что можно ввести — данные за июнь-июль остаются в БД
# нетронутыми. Настройка, не хардкод — app_setting.display_window_start/
# _end, редактируется на /settings, меняется без деплоя.
DEFAULT_DISPLAY_WINDOW_START = date_cls(2026, 8, 1)
DEFAULT_DISPLAY_WINDOW_END = date_cls(2026, 11, 28)


def get_display_window():
    start_v = get_app_setting("display_window_start")
    end_v = get_app_setting("display_window_end")
    try:
        start = date_cls.fromisoformat(start_v) if start_v else DEFAULT_DISPLAY_WINDOW_START
    except ValueError:
        start = DEFAULT_DISPLAY_WINDOW_START
    try:
        end = date_cls.fromisoformat(end_v) if end_v else DEFAULT_DISPLAY_WINDOW_END
    except ValueError:
        end = DEFAULT_DISPLAY_WINDOW_END
    if start > end:
        start, end = end, start
    return start, end


def record_forecast_snapshot(today, forecast_date, method, remaining_effort_days, avg_daily_pace):
    """Копит снимок прогноза на текущую ISO-неделю (не чаще одного в неделю
    на метод) — не отдельный cron, снимок пишется при первом открытии
    /status на этой неделе. Если за неделю никто не откроет /status,
    снимка не будет — известное ограничение, см. migrations/004."""
    iso_year, iso_week, _ = today.isocalendar()
    execute(
        """
        insert into forecast_snapshot
            (snapshot_date, iso_year, iso_week, forecast_date, method, remaining_effort_days, avg_daily_pace)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (iso_year, iso_week, method) do nothing
        """,
        (today, iso_year, iso_week, forecast_date, method, remaining_effort_days, avg_daily_pace),
    )


def get_scurve_data():
    """
    Данные экрана "Успеваем?" (докладная координатора "что делают
    отраслевые системы", 16.08.2026): S-кривая план/факт нарастающим
    итогом из человеко-дней (та же трудоёмкость, что и в EVM-слое),
    гистограмма численности по дням, прогноз завершения по темпу (единая
    функция — get_criticality_data(), 08.09.2026: раньше здесь стояла
    вторая, независимая формула, из-за которой /status и "Обзор"
    показывали разные даты), тренд прогноза по неделям (метод
    "baseline_lag" в тренде — историческая вторая оценка, оставлена только
    в графике тренда, не как текущий прогноз), дефицит ресурса до
    директивного срока.
    """
    today = object_today()
    evm = get_evm_data()
    crit = get_criticality_data()
    # Остаток трудоёмкости и прогноз по темпу считает get_criticality_data()
    # (единственное место, см. её докстринг) — здесь не пересчитываем.
    remaining = crit["remaining_effort_days"]
    known_count = crit["known_work_count"]
    excluded_count = crit["excluded_work_count"]

    daily = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual
        from latest_dp
        group by date
        order by date
        """
    )
    # Окно отображения (решение координатора 29.08.2026, 01.08-28.11 по
    # умолчанию) — нарастающий итог (bcws_cum/acwp_cum) считаем по ВСЕЙ
    # истории (иначе кривая на 01.08 стартовала бы с нуля, хотя реальный
    # прогресс с июня никуда не делся — это была бы неверная, не просто
    # "обрезанная" картинка), а в сам `series` (то, что рисует график)
    # добавляем только точки внутри окна. Данные за июнь-июль в БД не
    # трогаются, участвуют в сумме, просто не рисуются на оси.
    window_start, window_end = get_display_window()
    series = []
    bcws_cum = 0.0
    acwp_cum = 0.0
    for r in daily:
        bcws_cum += float(r["planned"] or 0)
        if r["actual"] is not None:
            acwp_cum += float(r["actual"])
        if r["date"] < window_start or r["date"] > window_end:
            continue
        series.append({
            "date": r["date"].isoformat(),
            "planned": float(r["planned"] or 0),
            "actual": float(r["actual"]) if r["actual"] is not None else None,
            "bcws_cum": round(bcws_cum, 1),
            "acwp_cum": round(acwp_cum, 1),
        })

    directive_deadline = get_directive_deadline()

    # Прогноз по темпу — тот же, что уже посчитан в crit["forecast_date"]
    # (см. докстринг get_criticality_data): не пересчитываем повторно,
    # только логируем снимок для графика тренда по неделям.
    forecast_pace_date = crit["forecast_date"]
    avg_pace = crit["avg_pace"]
    if avg_pace is not None:
        record_forecast_snapshot(today, forecast_pace_date, "pace", round(remaining, 1), avg_pace)
    if crit.get("baseline_lag_forecast_date"):
        record_forecast_snapshot(today, crit["baseline_lag_forecast_date"], "baseline_lag", None, None)

    trend_rows = query(
        "select snapshot_date, iso_year, iso_week, forecast_date, method "
        "from forecast_snapshot order by iso_year, iso_week"
    )
    trend = {"pace": [], "baseline_lag": []}
    for r in trend_rows:
        if r["forecast_date"] is None or r["method"] not in trend:
            continue
        trend[r["method"]].append({
            # Понедельник этой недели, не номер недели (правило проекта —
            # недели показываются датой, не ISO-номером).
            "week": date_cls.fromisocalendar(r["iso_year"], r["iso_week"], 1).isoformat(),
            "forecast_date": r["forecast_date"].isoformat(),
        })

    required_to_deadline = deadline_deficit = deadline_surplus = None
    if directive_deadline:
        # "Главное!!!" (координатор, 29.08.2026): график по контракту
        # начинается 01.09.2026 — считаем от более поздней из (сегодня,
        # директивный старт), не от голого "сегодня" (см.
        # schedule_calc_start()), иначе дни ДО начала графика ложно
        # увеличивают знаменатель и требуемая численность занижается.
        required_to_deadline, working_days_to_deadline = compute_required_people(
            remaining, schedule_calc_start(), directive_deadline
        )
        deadline_deficit, deadline_surplus, deadline_coverage_pct = compute_resource_deficit(required_to_deadline, crit.get("actual_crew"))
    else:
        working_days_to_deadline = None

    deviation_days = None
    if directive_deadline and forecast_pace_date:
        deviation_days = (forecast_pace_date - directive_deadline).days

    return {
        "today": today,
        "evm": evm,
        "crit": crit,
        "remaining_effort_days": round(remaining, 1),
        "known_work_count": known_count,
        "excluded_work_count": excluded_count,
        "series": series,
        "total_trudoemkost": evm.get("total_trudoemkost", 0),
        "bcwp_point": evm.get("bcwp", 0),
        "forecast_pace_date": forecast_pace_date.isoformat() if forecast_pace_date else None,
        "avg_pace": avg_pace,
        "trend": trend,
        "directive_deadline": directive_deadline.isoformat() if directive_deadline else None,
        "directive_start": get_directive_start(),
        "required_to_deadline": required_to_deadline,
        "working_days_to_deadline": working_days_to_deadline,
        "deadline_deficit": deadline_deficit,
        "deadline_surplus": deadline_surplus,
        "deadline_coverage_pct": deadline_coverage_pct,
        "deviation_days": deviation_days,
    }


@app.post("/api/settings/directive-deadline")
def set_directive_deadline(value: str = Form("")):
    """JSON-ответ, не редирект — форма на /status сохраняет через fetch
    (нужно показать «Сохранено» и включить/выключить кнопку по факту
    изменения, редирект с перезагрузкой всей страницы это не даёт)."""
    # Пункт 2, 30.08.2026: живой инцидент показал, что тело с пустым
    # value молча писало NULL поверх боевого значения — от этой даты
    # считается всё отставание проекта. Раньше пустое было легитимным
    # «очистить срок»; теперь — отклоняется, ничего не пишется.
    value = value.strip()
    if not value:
        return JSONResponse({"ok": False, "error": "Директивный срок не может быть пустым."}, status_code=400)
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Некорректная дата."}, status_code=400)
    execute(
        "insert into app_setting (key, value, updated_at) values ('directive_deadline', %s, now()) "
        "on conflict (key) do update set value=excluded.value, updated_at=now()",
        (value,),
    )
    return {"ok": True, "value": value}


@app.get("/status")
def status_page(request: Request):
    data = get_scurve_data()
    chart_payload = {
        "series": data["series"],
        "total_trudoemkost": data["total_trudoemkost"],
        "bcwp_point": data["bcwp_point"],
        "last_actual_date": data["evm"].get("last_actual_date").isoformat() if data["evm"].get("last_actual_date") else None,
        "forecast_pace_date": data["forecast_pace_date"],
        "trend": data["trend"],
        # Часть 2, 30.08.2026: график тренда теперь считает отклонение от
        # директивного срока (не абсолютную дату) — нужен сам срок на
        # клиенте, раньше он был только в контексте шаблона, не в JSON.
        "directive_deadline": data["directive_deadline"],
    }
    data["chart_json"] = json.dumps(chart_payload, ensure_ascii=False)
    return render(request, "status.html", "status", **data)


@app.get("/api/status-data")
def api_status_data():
    return get_scurve_data()


# ---------------------------------------------------------------------
# Экран "Что делать сегодня" (реинжиниринг v3, Цикл 2) — look-ahead,
# критичные работы по явному правилу, ограничения к снятию.
# ---------------------------------------------------------------------

def get_lookahead_works(today, horizon_days=14):
    """
    Работы, которые должны начаться в ближайшие horizon_days по baseline
    (только high/medium confidence — та же дисциплина, что и в /critical).
    "Что мешает начаться" — честно по тому, что реально есть в БД: сейчас
    это только "нет привязанного субподрядчика" (subcontractor_id) — реестры
    blocker/material не привязаны к конкретным работам (см.
    REENGINEERING_LOG.md, Цикл 2) и материал не проверяется, чтобы не
    придумывать сигнал, которого нет.
    """
    end = today + timedelta(days=horizon_days)
    rows = query(
        f"""
        select w.id, w.code, w.name, w.location, w.executor_type, w.subcontractor_id, bs.plan_start
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_start between %s and %s
          and bs.confidence in ('high', 'medium')
          and {_work_status_expr()} = 'not_started'
        order by bs.plan_start, w.code
        """,
        (today, end),
    )
    result = []
    for w in rows:
        obstacles = []
        if w["executor_type"] == "subcontract" and w["subcontractor_id"] is None:
            obstacles.append("нет привязанного субподрядчика")
        result.append({**w, "obstacles": obstacles})
    return result


def get_critical_rule_works(today, directive_deadline, trudoemkost_by_work, last_actual_by_work):
    """
    Критичность по явному правилу (не абстрактный "приоритет 1..5" из
    Excel): работа попадает в список, если
      1) уже просрочена относительно baseline, ИЛИ
      2) плановое окончание позже директивного срока проекта, ИЛИ
      3) на неё напрямую висит неснятое ограничение (work_id указан).
    Для каждой — оценка требуемого темпа и нехватки людей ПО ЭТОЙ работе
    (тот же принцип, что и в общем калькуляторе Дата↔Люди, но на уровне
    одной работы вместо всего проекта).
    """
    works = query(
        f"""
        select w.id, w.code, w.name, w.fact_pct, {_work_status_expr()} as status, bs.plan_finish
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_finish is not null and bs.confidence in ('high', 'medium')
        """
    )
    blocked_counts = {
        r["work_id"]: r["n"] for r in query(
            "select work_id, count(*) as n from blocker where work_id is not null and status='active' group by work_id"
        )
    }
    # "Просрочена" — тот же compute_overdue(), что и в get_criticality_data()
    # (координатор, 08.09.2026: раньше сравнение plan_finish < today было
    # реализовано здесь заново инлайн — тот же вопрос, отдельная копия).
    overdue_codes = {w["code"] for w in compute_overdue(works, today)}
    result = []
    for w in works:
        if w["status"] in DONE_STATUSES:
            continue
        reasons = []
        if w["code"] in overdue_codes:
            reasons.append("просрочена относительно планового срока")
        if directive_deadline and w["plan_finish"] > directive_deadline:
            reasons.append("плановое окончание позже директивного срока проекта")
        blocked_n = blocked_counts.get(w["id"], 0)
        if blocked_n:
            reasons.append(f"висит {blocked_n} неснятых ограничений")
        if not reasons:
            continue

        additional_people = None
        trud = trudoemkost_by_work.get(w["id"])
        if trud is not None:
            pct = float(w["fact_pct"]) if w["fact_pct"] is not None else 0.0
            remaining_work = float(trud) * (1 - pct / 100)
            targets = [w["plan_finish"]] + ([directive_deadline] if directive_deadline else [])
            target = min(targets)
            required_pace, _ = compute_required_people(remaining_work, today, target)
            if required_pace is not None:
                current_pace = last_actual_by_work.get(w["id"], 0)
                additional_people = max(0, required_pace - current_pace)

        result.append({**w, "reasons": reasons, "additional_people": additional_people})
    return sorted(result, key=lambda x: -(x["additional_people"] or 0))


def get_today_data():
    today = object_today()
    directive_deadline = get_directive_deadline()

    trudoemkost_rows = query(
        "select work_id, sum(planned_crew) as t from daily_progress "
        "where source='excel_import' and planned_crew is not null group by work_id"
    )
    trudoemkost_by_work = {r["work_id"]: r["t"] for r in trudoemkost_rows}

    last_actual_rows = query(
        LATEST_DP_CTE + """
        select distinct on (work_id) work_id, actual_crew
        from latest_dp where actual_crew is not null
        order by work_id, date desc
        """
    )
    last_actual_by_work = {r["work_id"]: r["actual_crew"] for r in last_actual_rows}

    lookahead = get_lookahead_works(today)
    critical = get_critical_rule_works(today, directive_deadline, trudoemkost_by_work, last_actual_by_work)
    open_blockers = query(
        "select id, blocker_type, description, created_at, expected_resolution_date, responsible_name, impact_days "
        "from blocker where status='active' order by created_at"
    )

    return {
        "today": today,
        "directive_deadline": directive_deadline.isoformat() if directive_deadline else None,
        "lookahead": lookahead,
        "critical": critical,
        "open_blockers": open_blockers,
    }


@app.get("/today")
def today_page(request: Request):
    return render(request, "today.html", "today", **get_today_data())


@app.post("/api/blocker/{blocker_id}")
def api_blocker_update(
    request: Request,
    blocker_id: int,
    expected_resolution_date: str = Form(""),
    responsible_name: str = Form(""),
    resolve: str = Form(""),
):
    # Права по веткам, 30.08.2026 — стоп-факторы (СМР), раньше вообще
    # не проверялись (только вход через middleware). См. has_permission().
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "error": "Доступ только для группы СМР."}, status_code=403)
    if resolve.strip():
        execute(
            "update blocker set status='resolved', actual_resolution_date=%s where id=%s",
            (object_today(), blocker_id),
        )
    else:
        d = expected_resolution_date.strip()
        if d:
            try:
                date_cls.fromisoformat(d)
            except ValueError:
                return JSONResponse({"error": "Некорректная дата"}, status_code=400)
        execute(
            "update blocker set expected_resolution_date=%s, responsible_name=%s where id=%s",
            (d or None, responsible_name.strip() or None, blocker_id),
        )
    return RedirectResponse("/today", status_code=303)


# ---------------------------------------------------------------------
# Суточный рапорт как документ (реинжиниринг v3, Цикл 3) — не экран, а
# датированная страница, пригодная для печати/PDF (window.print()) и
# приложения к переписке с заказчиком. Формируется из уже введённого
# факта + двух ручных полей (погода, подпись), которых Excel не даёт.
# ---------------------------------------------------------------------

@app.get("/report")
def report_page(request: Request, date: str = ""):
    if not date.strip():
        last = query_one("select max(date) as d from daily_progress where actual_crew is not null")
        target_date = last["d"] if last and last["d"] else object_today()
    else:
        try:
            target_date = date_cls.fromisoformat(date.strip())
        except ValueError:
            target_date = object_today()

    by_location = query(
        LATEST_DP_CTE + """
        select coalesce(w.location, 'без участка') as location,
               sum(ldp.planned_crew) as planned, sum(ldp.actual_crew) as actual
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s
        group by coalesce(w.location, 'без участка')
        order by location
        """,
        (target_date,),
    )
    # Знак "Недобора" (координатор, 31.08.2026) — тот же принцип, что на
    # "Обзоре"/"/resources"/"/status": раньше шаблон печатал голую
    # разность план-факт прямо в Jinja, включая отрицательные значения
    # под подписью "Недобор". Теперь недобор и избыток по участку
    # считаются раздельно и никогда не отрицательны.
    for r in by_location:
        diff = (r["planned"] or 0) - (r["actual"] or 0)
        r["deficit"] = diff if diff > 0 else None
        r["surplus"] = -diff if diff < 0 else None
    works_today = query(
        LATEST_DP_CTE + """
        select w.code, w.name, w.unit, ldp.planned_crew, ldp.actual_crew,
               ldp.done_volume, ldp.fact_pct, ldp.comment, ldp.reason_code, ldp.source
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s and (ldp.planned_crew is not null or ldp.actual_crew is not null)
        order by w.code
        """,
        (target_date,),
    )
    not_done = [
        r for r in works_today
        if (r["planned_crew"] or 0) > 0 and (r["actual_crew"] or 0) < (r["planned_crew"] or 0)
    ]
    blockers_arose = query(
        "select blocker_type, description from blocker where created_at::date = %s order by id",
        (target_date,),
    )
    blockers_resolved = query(
        "select blocker_type, description from blocker where actual_resolution_date = %s order by id",
        (target_date,),
    )
    meta = query_one("select weather, signed_by from daily_report_meta where date=%s", (target_date,))

    # Погода ещё не сохранялась вручную для этой даты (нет строки или поле
    # пустое) — предлагаем автоподстановку из daily_weather (см. задачу про
    # накопление погоды). Если для даты уже сохранено значение — оно и есть
    # то, что видел и подтвердил (или поправил) ответственный, автоподстановка
    # его не перезаписывает.
    weather_value = meta["weather"] if meta and meta["weather"] else None
    if weather_value is None:
        w = query_one(
            "select temp_09_c, precipitation_09_mm, wind_09_ms, weathercode_09 "
            "from daily_weather where date=%s and status='ok'",
            (target_date,),
        )
        # format_auto_weather вернёт None, если для даты нет замера на
        # 09:00 (даже если суточный агрегат есть) — задание координатора
        # прямо запрещает молча подставлять суточный агрегат вместо него.
        weather_value = (format_auto_weather(w) if w else None) or ""

    weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return render(
        request, "report.html", "report",
        target_date=target_date, date_label=f"{target_date.strftime('%d.%m.%Y')} ({weekday_names[target_date.weekday()]})",
        by_location=by_location, works_today=works_today, not_done=not_done,
        blockers_arose=blockers_arose, blockers_resolved=blockers_resolved,
        weather=weather_value, signed_by=meta["signed_by"] if meta else "",
    )


@app.post("/api/report-meta")
def api_report_meta(request: Request, date: str = Form(...), weather: str = Form(""), signed_by: str = Form("")):
    # Права по веткам, 30.08.2026 — рапорт (СМР), раньше не проверялось.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "error": "Доступ только для группы СМР."}, status_code=403)
    execute(
        "insert into daily_report_meta (date, weather, signed_by, updated_at) values (%s, %s, %s, now()) "
        "on conflict (date) do update set weather=excluded.weather, signed_by=excluded.signed_by, updated_at=now()",
        (date, weather.strip() or None, signed_by.strip() or None),
    )
    return RedirectResponse(f"/report?date={date}", status_code=303)


# ---------------------------------------------------------------------
# "Почему отстаём" — аналитика потерь (реинжиниринг v3, Цикл 3)
# ---------------------------------------------------------------------

def get_losses_data(period_days=30):
    today = object_today()
    start = today - timedelta(days=period_days)

    # Потери чел-дней по типу дня-уровневого ограничения. Один день может
    # нести несколько типов причин одновременно (ТЗ 11.4: "множественные
    # причины простоя в одной ячейке" — известный дефект исходных данных,
    # не подавляем его искусственным выбором "только одна причина").
    day_deficits = {
        r["date"]: max(0, float(r["planned"] or 0) - float(r["actual"] or 0))
        for r in query(
            LATEST_DP_CTE + """
            select date, sum(planned_crew) as planned, sum(actual_crew) as actual
            from latest_dp where date between %s and %s group by date
            """,
            (start, today),
        )
    }
    blocker_days = query(
        "select blocker_type, created_at::date as d from blocker where created_at::date between %s and %s",
        (start, today),
    )
    loss_by_type = {}
    overlap_days = 0
    seen_days = set()
    for b in blocker_days:
        loss_by_type.setdefault(b["blocker_type"], 0.0)
        loss_by_type[b["blocker_type"]] += day_deficits.get(b["d"], 0.0)
        if b["d"] in seen_days:
            overlap_days += 1
        seen_days.add(b["d"])

    # PPC по неделям
    ppc_rows = query(
        LATEST_DP_CTE + "select date, planned_crew, actual_crew from latest_dp where date between %s and %s",
        (start, today),
    )
    by_week = {}
    for r in ppc_rows:
        wk = r["date"].isocalendar()
        # Ключ — понедельник этой недели (ISO-дата сортируется так же, как
        # номер недели, но правило проекта требует показывать датой, не
        # номером — см. CLAUDE.md).
        key = date_cls.fromisocalendar(wk[0], wk[1], 1).isoformat()
        by_week.setdefault(key, []).append(r)
    ppc_by_week = []
    for wk in sorted(by_week):
        pct, promised, met = compute_ppc(by_week[wk])
        if promised:
            ppc_by_week.append({"week": wk, "ppc_pct": pct, "promised": promised, "met": met})

    # Срок снятия ограничений
    resolved = query(
        "select created_at::date as created, actual_resolution_date as resolved "
        "from blocker where status='resolved' and actual_resolution_date is not null"
    )
    resolution_days = [(r["resolved"] - r["created"]).days for r in resolved]
    avg_resolution_days = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None
    open_blocker_ages = [
        (today - r["created_at"].date()).days
        for r in query("select created_at from blocker where status='active'")
    ]

    # Отвлечение ресурса: сколько чел-дней факта ушло не на main
    by_source = query(
        """
        select w.source, sum(dp.planned_crew) as planned, sum(dp.actual_crew) as actual
        from daily_progress dp join work w on w.id = dp.work_id
        where dp.source = 'excel_import'
        group by w.source
        """
    )

    return {
        "period_days": period_days,
        "start": start,
        "today": today,
        "loss_by_type": sorted(
            [{"type": k, "days": round(v, 1)} for k, v in loss_by_type.items()],
            key=lambda x: -x["days"],
        ),
        "overlap_days": overlap_days,
        "ppc_by_week": ppc_by_week,
        "avg_resolution_days": avg_resolution_days,
        "resolved_count": len(resolution_days),
        "open_blocker_count": len(open_blocker_ages),
        "open_blocker_avg_age": round(sum(open_blocker_ages) / len(open_blocker_ages), 1) if open_blocker_ages else None,
        "by_source": [
            {
                "source": r["source"],
                "planned": float(r["planned"] or 0),
                "actual": float(r["actual"] or 0),
            }
            for r in by_source
        ],
    }


@app.get("/losses")
def losses_page(request: Request, period_days: int = 30):
    return render(request, "losses.html", "losses", **get_losses_data(period_days))


# ---------------------------------------------------------------------
# Главная
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Главная страница — редирект на панель координатора
# (упрощённая форма simple.html удалена; ввод факта через /form)
# ---------------------------------------------------------------------

@app.get("/")
def root_redirect():
    return RedirectResponse(url="/dashboard", status_code=302)

# ---------------------------------------------------------------------
# Критичные работы (ТЗ 4.3) — просрочка относительно временного baseline
# ---------------------------------------------------------------------

@app.get("/critical")
def critical(request: Request):
    crit = get_criticality_data()
    return render(request, "critical.html", "data", crit=crit)


# ---------------------------------------------------------------------
# Реестр работ
# ---------------------------------------------------------------------

@app.get("/works")
def works(request: Request, source: str = "", status: str = "", executor_type: str = "", q: str = ""):
    # Статус — вычисляется из fact_pct (_work_status_expr), не читает
    # столбец work.status (координатор, 08.09.2026, аудит целостности:
    # /works и /dashboard одновременно показывали разные распределения
    # по одному и тому же критерию).
    status_expr = _work_status_expr(None)
    sql = f"select code, source, location, name, unit, {status_expr} as status, executor_type, fact_pct, fact_pct_raw, data_quality_flag from work where true"
    params = []
    if source:
        sql += " and source=%s"; params.append(source)
    if status:
        sql += f" and {status_expr}=%s"; params.append(status)
    if executor_type:
        sql += " and executor_type=%s"; params.append(executor_type)
    if q:
        sql += " and name ilike %s"; params.append(f"%{q}%")
    sql += " order by code"
    rows = query(sql, params)

    sources = query("select distinct source from work order by source")
    statuses = query(f"select distinct {status_expr} as status from work order by 1")

    return render(
        request, "works.html", "data",
        rows=rows, sources=sources, statuses=statuses,
        f_source=source, f_status=status, f_executor=executor_type, f_q=q,
    )


# ---------------------------------------------------------------------
# Справочник норм трудозатрат — ДВА источника (оба описаны в CLAUDE.md,
# разделы «Справочник норм трудозатрат» и «Второй справочник —
# СТО-ССР»). Основной — ssr_norm (СТО-ССР-2026, Spider Project,
# внутренний норматив подрядчика ООО «ССР», разделы работ совпадают со
# scope ТМ-35). Вспомогательный — gesn_norm (ГЭСН-2022, госнорма общего
# назначения, шире по охвату, но не привязана к реальной технике/
# бригадам этого подрядчика) — показывается только когда в основном
# справочнике по запросу ничего не нашлось. НИ ОДИН из двух каталогов
# не связан с конкретными работами ПТО — единица измерения у
# большинства из 163 работ Excel «комп.», не физический объём, считать
# трудозатраты нечем (см. CLAUDE.md, «Истории замен» пп. 3-4 —
# сопоставление уже пробовалось и не работает). Экран — поиск нормы +
# расчёт на объём, введённый человеком вручную, который знает реальный
# объём своей работы.
# ---------------------------------------------------------------------

NORMS_RESULT_LIMIT = 300


@app.get("/norms")
def norms(request: Request, q: str = "", section: str = ""):
    sections = query("select distinct section from ssr_norm order by section")

    ssr_rows = []
    ssr_total = 0
    if q or section:
        sql = "select section, code, name, unit, labor_hours_per_unit from ssr_norm where true"
        params = []
        if q:
            sql += " and name ilike %s"
            params.append(f"%{q}%")
        if section:
            sql += " and section=%s"
            params.append(section)
        ssr_total = query_one(f"select count(*) as n from ({sql}) t", params)["n"]
        sql += " order by section, code limit %s"
        params.append(NORMS_RESULT_LIMIT)
        ssr_rows = query(sql, params)

    gesn_rows = []
    gesn_total = 0
    if (q or section) and ssr_total == 0:
        sql = "select sbornik_title, code, name, unit, hours_per_unit from gesn_norm where true"
        params = []
        if q:
            sql += " and name ilike %s"
            params.append(f"%{q}%")
        gesn_total = query_one(f"select count(*) as n from ({sql}) t", params)["n"]
        sql += " order by sbornik_title, code limit %s"
        params.append(NORMS_RESULT_LIMIT)
        gesn_rows = query(sql, params)

    return render(
        request, "norms.html", "data",
        ssr_rows=ssr_rows, ssr_total=ssr_total, sections=sections,
        gesn_rows=gesn_rows, gesn_total=gesn_total,
        f_q=q, f_section=section, result_limit=NORMS_RESULT_LIMIT,
    )


# ---------------------------------------------------------------------
# Плановый график по 56 нормированным позициям сметы (docs/
# smeta_normalization_test_2026-08-19.md). Расчёт срока — НЕЗАВИСИМО по
# каждой позиции (если на эту работу выделить N человек с даты начала,
# когда закончится) — общий пул людей между позициями НЕ моделируется,
# это явное упрощение MVP, координатор попросил именно «по работам».
# Трудозатраты чел-час -> чел-дни через 8-часовой рабочий день (тот же
# принцип, что уже в /api/calculator).
# ---------------------------------------------------------------------

HOURS_PER_DAY = 8


def _norm_plan_render(request: Request, errors=None, ok=False):
    rows = query("select * from norm_plan_item order by smeta_n")
    start_raw = get_app_setting("norm_plan_start")
    start = date_cls.fromisoformat(start_raw) if start_raw else object_today()

    total_hours = 0.0
    total_assigned = 0
    out_rows = []
    for r in rows:
        row = dict(r)
        total_hours += float(row["labor_hours_total"] or 0)
        working_days = forecast_date = None
        if row["assigned_people"]:
            total_assigned += 1
            remaining_days = float(row["labor_hours_total"]) / HOURS_PER_DAY
            working_days, forecast_date = compute_forecast_from_people(
                remaining_days, row["assigned_people"], start
            )
        row["working_days"] = working_days
        row["forecast_date"] = forecast_date
        out_rows.append(row)

    return render(
        request, "norm_plan.html", "data",
        rows=out_rows, start=start.isoformat(),
        total_hours=round(total_hours, 1), total_assigned=total_assigned, total_n=len(out_rows),
        errors=errors or [], ok=ok,
    )


@app.get("/norm-plan")
def norm_plan_page(request: Request):
    return _norm_plan_render(request)


@app.post("/norm-plan")
async def norm_plan_save(request: Request):
    # Права по веткам, 30.08.2026 — плановый график по нормам, тот же
    # класс СМР-планирования, что /baseline. Раньше не проверялось.
    # HTML-рендер той же функцией, что и остальные ошибки этой формы.
    if not has_permission(request.state.user, "smr:write"):
        return _norm_plan_render(request, errors=["Доступ только для группы СМР."])
    form = await request.form()
    # Пункт 2, 30.08.2026: тот же класс бага, что вскрыл живой инцидент на
    # /api/settings/directive-deadline — пустая/некорректная дата раньше
    # молча писала NULL поверх уже заданного начала графика (используется
    # расчётом срока по всем 56 позициям сметы). Теперь — явная ошибка,
    # ничего не пишется, ни в app_setting, ни построчно ниже.
    start_raw = (form.get("start") or "").strip()
    if not start_raw:
        return _norm_plan_render(request, errors=["«Дата начала работ» не может быть пустой."])
    try:
        date_cls.fromisoformat(start_raw)
    except ValueError:
        return _norm_plan_render(request, errors=["«Дата начала работ» указана некорректно."])
    execute(
        "insert into app_setting (key, value, updated_at) values ('norm_plan_start', %s, now()) "
        "on conflict (key) do update set value=excluded.value, updated_at=now()",
        (start_raw,),
    )

    rows = query("select id from norm_plan_item")
    for r in rows:
        raw = (form.get(f"people_{r['id']}") or "").strip()
        people = None
        if raw:
            try:
                people = max(0, int(raw))
            except ValueError:
                people = None
        execute("update norm_plan_item set assigned_people=%s where id=%s", (people, r["id"]))

    return RedirectResponse("/norm-plan", status_code=303)


# ---------------------------------------------------------------------
# Ресурсы
# ---------------------------------------------------------------------

@app.get("/resources")
def resources(request: Request):
    rows = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual,
               count(*) filter (where actual_crew is not null) as works_with_fact
        from latest_dp
        group by date
        order by date
        """
    )
    # Знак "Дефицита" (координатор, 31.08.2026) — тот же принцип, что на
    # "Обзоре" (home_v2): раньше "дефицит" был голой разностью план-факт
    # прямо в SQL, включая отрицательные значения — при факте больше
    # плана строка показывала "-4" под подписью "Дефицит". Дефицит и
    # избыток теперь считаются раздельно и никогда не отрицательны;
    # при точном равенстве обе колонки — "-" (см. resources.html).
    for r in rows:
        diff = (r["planned"] or 0) - (r["actual"] or 0)
        r["deficit"] = diff if diff > 0 else None
        r["surplus"] = -diff if diff < 0 else None
    resource_pool_rows = query_one("select count(*) as n from resource_pool")["n"]
    return render(request, "resources.html", "data", rows=rows, resource_pool_rows=resource_pool_rows)


# ---------------------------------------------------------------------
# Простои
# ---------------------------------------------------------------------

@app.get("/downtime")
def downtime(request: Request):
    rows = query(
        LATEST_DP_CTE + """
        select ldp.date, w.code, w.name, ldp.comment, ldp.planned_crew, ldp.actual_crew
        from latest_dp ldp
        join work w on w.id = ldp.work_id
        where ldp.comment is not null and ldp.comment <> ''
        order by ldp.date desc
        limit 200
        """
    )
    total_with_comment = query_one(
        LATEST_DP_CTE + "select count(*) as n from latest_dp where comment is not null and comment <> ''"
    )["n"]
    reason_coded = query_one(
        "select count(*) as n from daily_progress where reason_code is not null"
    )["n"]
    return render(
        request, "downtime.html", "data",
        rows=rows, total_with_comment=total_with_comment, reason_coded=reason_coded,
    )


# ---------------------------------------------------------------------
# Субподрядчики
# ---------------------------------------------------------------------

@app.get("/subcontractors")
def subcontractors(request: Request, ok: str = ""):
    registry_rows = query("select * from subcontractor order by name")
    proxy_rows = query(
        "select code, name, comment, location from work "
        "where executor_type='subcontract' order by code"
    )
    return render(
        request, "subcontractors.html", "data",
        registry_rows=registry_rows, proxy_rows=proxy_rows,
        errors=[], ok=bool(ok), values={},
    )


# ====== POST /subcontractors — добавить субподрядную организацию ======
# До 28.08.2026 реестр (`subcontractor`, 0 строк) существовал только как
# пустая таблица — insert в коде не было вообще, показывались только
# работы с executor_type='subcontract' (proxy_rows выше, без привязки к
# юрлицу). Не трогает proxy_rows/work.subcontractor_id — отдельный путь.
@app.post("/subcontractors")
def subcontractors_post(
    request: Request,
    name: str = Form(""),
    work_type: str = Form(""),
    contract_status: str = Form(""),
    mobilization_status: str = Form(""),
    expected_start_date: str = Form(""),
    actual_start_date: str = Form(""),
    crew_size: str = Form(""),
    reason_delayed: str = Form(""),
    impact: str = Form(""),
    comment: str = Form(""),
):
    errors = []
    # Права по веткам, 30.08.2026 — реестр субподрядчиков (СМР), раньше
    # не проверялось. Через общий "if errors" ниже — та же HTML-форма
    # рендерит остальные ошибки валидации, не JSON.
    if not has_permission(request.state.user, "smr:write"):
        errors.append("Доступ только для группы СМР.")
    name_val = name.strip()
    if not name_val:
        errors.append("«Организация» обязательна.")

    exp_start_val = None
    if expected_start_date.strip():
        try:
            exp_start_val = date_cls.fromisoformat(expected_start_date.strip())
        except ValueError:
            errors.append("«Начало план» указано некорректно.")

    act_start_val = None
    if actual_start_date.strip():
        try:
            act_start_val = date_cls.fromisoformat(actual_start_date.strip())
        except ValueError:
            errors.append("«Начало факт» указано некорректно.")

    crew_val = validate_crew(crew_size, "Бригада", errors)

    if errors:
        registry_rows = query("select * from subcontractor order by name")
        proxy_rows = query(
            "select code, name, comment, location from work "
            "where executor_type='subcontract' order by code"
        )
        return render(
            request, "subcontractors.html", "data",
            registry_rows=registry_rows, proxy_rows=proxy_rows, errors=errors, ok=False,
            values={
                "name": name, "work_type": work_type, "contract_status": contract_status,
                "mobilization_status": mobilization_status, "expected_start_date": expected_start_date,
                "actual_start_date": actual_start_date, "crew_size": crew_size,
                "reason_delayed": reason_delayed, "impact": impact, "comment": comment,
            },
        )

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            """
            insert into subcontractor
                (name, work_type, contract_status, mobilization_status, expected_start_date,
                 actual_start_date, crew_size, reason_delayed, impact, comment)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (name_val, work_type.strip() or None, contract_status.strip() or None,
             mobilization_status.strip() or None, exp_start_val, act_start_val, crew_val,
             reason_delayed.strip() or None, impact.strip() or None, comment.strip() or None),
        )
        sub_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'subcontractor', %s, 'subcontractor_create', "
            "jsonb_build_object('name', %s, 'work_type', %s, 'contract_status', %s), "
            "'форма /subcontractors')",
            (user_id, sub_id, name_val, work_type.strip() or None, contract_status.strip() or None),
        )

    run_in_transaction(_do)
    return RedirectResponse(url="/subcontractors?ok=1", status_code=303)


# ---------------------------------------------------------------------
# Настройки объекта — до 28.08.2026 директивный срок (и координаты для
# погоды) правились исключительно прямым запросом в БД. Единственный
# существующий эндпоинт (`POST /api/settings/directive-deadline`) был
# рассчитан на форму на странице `/status`, которой в текущем коде нет
# (`templates/status.html` отсутствует в контейнере, маршрута нет ни в
# одном пункте меню — мёртвый хвост более раннего цикла реинжиниринга,
# см. опись находок). Не трогаю ни `/status`, ни старый JSON-эндпоинт —
# оставлены как есть (правило "не чистить"), просто у направленного на
# них функционала до сих пор не было работающей формы. Новая страница
# ниже сохраняет полной перезагрузкой (как остальные формы этой сессии),
# не через fetch.
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Журнал входов и действий — учётные записи, 29.08.2026. Только для
# координатора (admin) — здесь видно, кто когда пытался войти (включая
# неудачные попытки) и кто что менял.
# ---------------------------------------------------------------------

@app.get("/journal")
def journal_page(request: Request, user_id: str = "", date_from: str = "", date_to: str = ""):
    if not is_admin(request.state.user):
        return RedirectResponse(url="/login?next=/journal", status_code=303)

    where = []
    params = []
    if user_id.strip():
        where.append("l.user_id = %s")
        params.append(int(user_id))
    if date_from.strip():
        where.append("l.created_at::date >= %s")
        params.append(date_from.strip())
    if date_to.strip():
        where.append("l.created_at::date <= %s")
        params.append(date_to.strip())
    where_sql = ("where " + " and ".join(where)) if where else ""

    logins = query(
        f"select l.id, l.login_attempted, l.user_id, u.full_name, l.success, l.reason, l.ip, l.created_at "
        f"from login_log l left join app_user u on u.id=l.user_id {where_sql} "
        f"order by l.created_at desc limit 200",
        params,
    )

    where_a = []
    params_a = []
    if user_id.strip():
        where_a.append("a.user_id = %s")
        params_a.append(int(user_id))
    if date_from.strip():
        where_a.append("a.created_at::date >= %s")
        params_a.append(date_from.strip())
    if date_to.strip():
        where_a.append("a.created_at::date <= %s")
        params_a.append(date_to.strip())
    where_a_sql = ("where " + " and ".join(where_a)) if where_a else ""
    actions = query(
        f"select a.id, a.user_id, u.full_name, a.entity_type, a.entity_id, a.action, a.reason, a.created_at "
        f"from audit_log a left join app_user u on u.id=a.user_id {where_a_sql} "
        f"order by a.created_at desc limit 200",
        params_a,
    )

    users = query("select id, full_name from app_user where login is not null order by full_name")
    return render(
        request, "journal.html", "journal",
        logins=logins, actions=actions, users=users,
        f_user_id=user_id, f_date_from=date_from, f_date_to=date_to,
    )


# ---------------------------------------------------------------------
# Смена пароля координатором — учётные записи, 29.08.2026.
# ---------------------------------------------------------------------

@app.get("/settings/users")
def settings_users_page(request: Request, ok: str = ""):
    if not is_admin(request.state.user):
        return RedirectResponse(url="/login?next=/settings/users", status_code=303)
    users = query(
        "select u.id, u.full_name, u.login, u.password_changed_at, "
        "string_agg(p.permission, ', ' order by p.permission) as perms "
        "from app_user u left join user_permission p on p.user_id=u.id "
        "where u.login is not null group by u.id, u.full_name, u.login, u.password_changed_at "
        "order by u.full_name"
    )
    return render(request, "settings_users.html", "settings-users", users=users, ok=bool(ok))


@app.post("/settings/users/{target_user_id}/password")
def settings_users_password_post(request: Request, target_user_id: int, new_password: str = Form(...)):
    if not is_admin(request.state.user):
        return JSONResponse({"ok": False, "error": "Нет доступа."}, status_code=403)
    pw = new_password.strip()
    if len(pw) != 8 or not re.match(r"^[a-z0-9]{8}$", pw.lower()) or CYRILLIC_RE.search(pw):
        return RedirectResponse(url="/settings/users?ok=0", status_code=303)
    pw_hash, salt = hash_password(pw)
    admin_id = request.state.user["id"]

    def _do(cur):
        cur.execute(
            "update app_user set password_hash=%s, password_salt=%s, password_changed_at=now() where id=%s",
            (pw_hash, salt, target_user_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, reason) "
            "values (%s, 'app_user', %s, 'password_reset', 'форма /settings/users, сменил координатор')",
            (admin_id, target_user_id),
        )
        # Смена пароля обесценивает все действующие сессии этого человека —
        # если пароль меняли не по его просьбе, старые устройства не
        # должны остаться залогинены.
        cur.execute("delete from user_session where user_id=%s", (target_user_id,))

    run_in_transaction(_do)
    return RedirectResponse(url="/settings/users?ok=1", status_code=303)


SETTINGS_KEYS = (
    "directive_deadline", "object_lat", "object_lon",
    "display_window_start", "display_window_end",
)


@app.get("/settings")
def settings_page(request: Request, ok: str = ""):
    rows = query("select key, value from app_setting where key = any(%s)", (list(SETTINGS_KEYS),))
    values = {r["key"]: r["value"] for r in rows}
    return render(request, "settings.html", "settings", errors=[], ok=bool(ok), values=values)


@app.post("/settings")
def settings_post(
    request: Request,
    directive_deadline: str = Form(""),
    object_lat: str = Form(""),
    object_lon: str = Form(""),
    display_window_start: str = Form(""),
    display_window_end: str = Form(""),
):
    # Учётные записи, 29.08.2026: срок объекта/координаты/окно
    # отображения — объектовые параметры, не личные. Меняет координатор.
    if not is_admin(request.state.user):
        return JSONResponse({"ok": False, "error": "Только координатор может менять настройки объекта."}, status_code=403)

    # Пункт 2, 30.08.2026: тот же класс бага, что вскрыл живой инцидент на
    # /api/settings/directive-deadline — все пять полей раньше молча
    # принимали пустое значение и затирали существующее (пустая строка →
    # None → перезаписывает БД). Форма всегда приходит с уже заполненными
    # текущими значениями (settings.html), поэтому пустое поле здесь —
    # верный признак сбойной отправки, не осознанного «очистить».
    errors = []

    deadline_val = None
    if not directive_deadline.strip():
        errors.append("«Директивный срок» не может быть пустым.")
    else:
        try:
            deadline_val = date_cls.fromisoformat(directive_deadline.strip())
        except ValueError:
            errors.append("«Директивный срок» указан некорректно.")

    lat_val = None
    if not object_lat.strip():
        errors.append("«Широта» не может быть пустой.")
    else:
        try:
            lat_val = float(object_lat.strip().replace(",", "."))
        except ValueError:
            errors.append("«Широта» должна быть числом.")
        else:
            if not (-90 <= lat_val <= 90):
                errors.append("«Широта» должна быть от -90 до 90.")

    lon_val = None
    if not object_lon.strip():
        errors.append("«Долгота» не может быть пустой.")
    else:
        try:
            lon_val = float(object_lon.strip().replace(",", "."))
        except ValueError:
            errors.append("«Долгота» должна быть числом.")
        else:
            if not (-180 <= lon_val <= 180):
                errors.append("«Долгота» должна быть от -180 до 180.")

    # Окно отображения графиков/диаграмм (решение координатора 29.08.2026)
    # — ТОЛЬКО про что показывается на графиках, не ограничивает ввод
    # факта задним числом и не трогает данные в БД (см. get_display_window()).
    win_start_val = None
    if not display_window_start.strip():
        errors.append("«Начало окна отображения» не может быть пустым.")
    else:
        try:
            win_start_val = date_cls.fromisoformat(display_window_start.strip())
        except ValueError:
            errors.append("«Начало окна отображения» указано некорректно.")

    win_end_val = None
    if not display_window_end.strip():
        errors.append("«Конец окна отображения» не может быть пустым.")
    else:
        try:
            win_end_val = date_cls.fromisoformat(display_window_end.strip())
        except ValueError:
            errors.append("«Конец окна отображения» указан некорректно.")

    if win_start_val and win_end_val and win_start_val > win_end_val:
        errors.append("«Начало окна отображения» позже «Конца» — проверьте даты.")

    if errors:
        rows = query("select key, value from app_setting where key = any(%s)", (list(SETTINGS_KEYS),))
        values = {r["key"]: r["value"] for r in rows}
        values.update({
            "directive_deadline": directive_deadline, "object_lat": object_lat, "object_lon": object_lon,
            "display_window_start": display_window_start, "display_window_end": display_window_end,
        })
        return render(request, "settings.html", "settings", errors=errors, ok=False, values=values)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        pairs = [
            ("directive_deadline", str(deadline_val) if deadline_val else None),
            ("object_lat", str(lat_val) if lat_val is not None else None),
            ("object_lon", str(lon_val) if lon_val is not None else None),
            ("display_window_start", str(win_start_val) if win_start_val else None),
            ("display_window_end", str(win_end_val) if win_end_val else None),
        ]
        for key, val in pairs:
            cur.execute(
                "insert into app_setting (key, value, updated_at) values (%s, %s, now()) "
                "on conflict (key) do update set value=excluded.value, updated_at=now()",
                (key, val),
            )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'app_setting', 0, 'settings_update', "
            "jsonb_build_object('directive_deadline', %s, 'object_lat', %s, 'object_lon', %s, "
            "'display_window_start', %s, 'display_window_end', %s), "
            "'форма /settings')",
            (user_id, str(deadline_val) if deadline_val else None,
             str(lat_val) if lat_val is not None else None, str(lon_val) if lon_val is not None else None,
             str(win_start_val) if win_start_val else None, str(win_end_val) if win_end_val else None),
        )

    run_in_transaction(_do)
    return RedirectResponse(url="/settings?ok=1", status_code=303)


# ---------------------------------------------------------------------
# Материалы и поставки
# ---------------------------------------------------------------------

@app.get("/materials")
def materials(request: Request):
    rows = query("select * from material order by name")
    return render(request, "materials.html", "data", rows=rows)


# ---------------------------------------------------------------------
# Ограничения
# ---------------------------------------------------------------------

@app.get("/blockers")
def blockers(request: Request, ok: str = ""):
    rows = query(
        "select b.*, w.code as work_code, w.name as work_name "
        "from blocker b left join work w on w.id=b.work_id "
        "order by b.created_at desc"
    )
    work_rows = query("select id, code, name from work order by code")
    return render(request, "blockers.html", "blockers", rows=rows, work_rows=work_rows,
                  blocker_types=RU_BLOCKER_TYPE.items(), errors=[], ok=bool(ok), values={})


# ====== POST /blockers — создать стоп-фактор ======
# До 28.08.2026 в коде не было ни одного `insert into blocker` — все 9
# строк попали разовым импортом (см. брифинг §6.5). Форма даёт первый
# рабочий путь создания записи, не трогая существующий
# POST /api/blocker/{id} (он только снимает/переносит срок у уже
# существующей строки — оставлен как есть).
@app.post("/blockers")
def blockers_post(
    request: Request,
    work_id: str = Form(""),
    blocker_type: str = Form(""),
    description: str = Form(""),
    expected_resolution_date: str = Form(""),
    responsible_name: str = Form(""),
):
    errors = []

    # Права по веткам, 30.08.2026 — стоп-факторы (СМР), раньше не
    # проверялось. Через общий "if errors" ниже — та же HTML-форма
    # рендерит остальные ошибки валидации, не JSON.
    if not has_permission(request.state.user, "smr:write"):
        errors.append("Доступ только для группы СМР.")

    work_id_val = None
    if work_id.strip():
        try:
            work_id_val = int(work_id)
        except ValueError:
            errors.append("«Работа» указана некорректно.")
        else:
            if not query_one("select id from work where id=%s", (work_id_val,)):
                errors.append("Выбранная работа не найдена в справочнике.")

    if blocker_type not in RU_BLOCKER_TYPE:
        errors.append("«Тип» обязателен и должен быть из списка.")

    desc_val = description.strip()
    if not desc_val:
        errors.append("«Описание» обязательно.")

    exp_date_val = None
    if expected_resolution_date.strip():
        try:
            exp_date_val = date_cls.fromisoformat(expected_resolution_date.strip())
        except ValueError:
            errors.append("«Ожидаемая дата снятия» указана некорректно.")

    resp_val = responsible_name.strip() or None

    if errors:
        rows = query(
            "select b.*, w.code as work_code, w.name as work_name "
            "from blocker b left join work w on w.id=b.work_id "
            "order by b.created_at desc"
        )
        work_rows = query("select id, code, name from work order by code")
        return render(
            request, "blockers.html", "blockers", rows=rows, work_rows=work_rows,
            blocker_types=RU_BLOCKER_TYPE.items(), errors=errors, ok=False,
            values={
                "work_id": work_id, "blocker_type": blocker_type, "description": description,
                "expected_resolution_date": expected_resolution_date, "responsible_name": responsible_name,
            },
        )

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            """
            insert into blocker
                (work_id, blocker_type, description, status, owner_id, created_at, expected_resolution_date, responsible_name)
            values (%s, %s, %s, 'active', %s, now(), %s, %s)
            returning id
            """,
            (work_id_val, blocker_type, desc_val, user_id, exp_date_val, resp_val),
        )
        blocker_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'blocker', %s, 'blocker_create', "
            "jsonb_build_object('work_id', %s, 'blocker_type', %s, 'description', %s, "
            "'expected_resolution_date', %s, 'responsible_name', %s), 'создано через форму /blockers')",
            (user_id, blocker_id, work_id_val, blocker_type, desc_val,
             str(exp_date_val) if exp_date_val else None, resp_val),
        )

    run_in_transaction(_do)
    return RedirectResponse(url="/blockers?ok=1", status_code=303)


# ---------------------------------------------------------------------
# Ежедневная сводка
# ---------------------------------------------------------------------

@app.get("/daily-report")
def daily_report(request: Request, date: str = ""):
    if not date:
        last = query_one(
            "select max(date) as d from daily_progress where actual_crew is not null"
        )
        date = str(last["d"]) if last["d"] else str(object_today())

    today_rows = query(
        LATEST_DP_CTE + """
        select w.code, w.name, ldp.planned_crew, ldp.actual_crew, ldp.comment, ldp.source
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = %s
        order by w.code
        """,
        (date,),
    )
    tomorrow_rows = query(
        LATEST_DP_CTE + """
        select w.code, w.name, ldp.planned_crew
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date = (%s::date + interval '1 day')::date and ldp.planned_crew > 0
        order by w.code
        """,
        (date,),
    )
    return render(
        request, "daily_report.html", "data",
        date=date, today_rows=today_rows, tomorrow_rows=tomorrow_rows,
    )


# ---------------------------------------------------------------------
# Обоснование Исполнителя
# ---------------------------------------------------------------------

@app.get("/executor")
def executor(request: Request):
    agg = query_one(
        LATEST_DP_CTE + """
        select sum(planned_crew) as total_planned, sum(actual_crew) as total_actual,
               round(avg(planned_crew),1) as avg_planned, round(avg(actual_crew),1) as avg_actual,
               count(distinct date) as days_with_data
        from latest_dp
        """
    )
    by_source_type = query("select source, count(*) as n from work group by source order by source")
    subcontract_count = query_one(
        "select count(*) as n from work where executor_type='subcontract'"
    )["n"]
    return render(
        request, "executor.html", "data",
        agg=agg, by_source_type=by_source_type, subcontract_count=subcontract_count,
    )


# ---------------------------------------------------------------------
# Качество данных
# ---------------------------------------------------------------------

@app.get("/quality")
def quality(request: Request):
    flagged_works = query(
        "select code, name, data_quality_note from work "
        "where data_quality_flag='needs_review' order by code"
    )
    flagged_daily = query(
        "select dp.date, w.code, dp.data_quality_note from daily_progress dp "
        "join work w on w.id=dp.work_id "
        "where dp.data_quality_flag='needs_review' order by dp.date"
    )
    unresolved = query(
        "select sheet, cell_ref, work_code, issue_type, raw_payload, resolved "
        "from import_unresolved_cell order by id"
    )
    # Подготовка пилота, 30.08.2026: raw_payload — JSONB, шаблон печатал
    # его как есть (str() от Python-словаря) — "'month': None" и т.п.
    # видел живой посетитель. Чистим None рекурсивно ТОЛЬКО для показа,
    # сами данные в БД не трогаем.
    for row in unresolved:
        row["raw_payload"] = _clean_none_for_display(row["raw_payload"])
    return render(
        request, "quality.html", "data",
        flagged_works=flagged_works, flagged_daily=flagged_daily, unresolved=unresolved,
    )


# ---------------------------------------------------------------------
# Веб-форма ежедневного факта (опциональный второй канал) — Цикл 2
# переработан с нуля: серверная валидация (клиентскую можно обойти),
# закрытый справочник причин простоя (ТЗ 8.8), предупреждение о дате
# сильно не "сегодня", проверка уже существующей записи за день.
# ---------------------------------------------------------------------


def validate_crew(raw, field_label, errors):
    """Целое число 0..50 или пусто. Текст/дробь/диапазон — явная ошибка,
    не тихий None (та же категория дефекта, что "16шт" в Excel — там
    валидации не было вообще, здесь обязана быть)."""
    if raw is None or raw.strip() == "":
        return None
    try:
        val = int(raw)
    except ValueError:
        errors.append(f"«{field_label}» должно быть целым числом (введено: {raw!r}).")
        return None
    if not (0 <= val <= 50):
        errors.append(f"«{field_label}» должно быть от 0 до 50 (введено: {val}).")
        return None
    return val


def validate_date(raw, errors, warnings):
    if not raw or not raw.strip():
        errors.append("«Дата» обязательна.")
        return None
    try:
        d = date_cls.fromisoformat(raw.strip())
    except ValueError:
        errors.append(f"«Дата» не распознана как дата (введено: {raw!r}).")
        return None
    delta = (d - object_today()).days
    if delta < -30:
        warnings.append(f"Дата {d} — более 30 дней в прошлом. Запись сохранится, но проверьте, не опечатка ли это.")
    elif delta > 30:
        warnings.append(f"Дата {d} — более 30 дней в будущем. Запись сохранится, но проверьте, не опечатка ли это.")
    return d


# Старый /api/existing-entry удалён — используется -v2 ниже


@app.get("/form")
def form_get(request: Request, ok: str = "", w: str = ""):
    work_rows = query("select id, code, name from work order by code")
    warnings = w.split("||") if w else []
    # Регламент Якименко А.И. (28.08.2026): форму заполняют на следующий
    # день до 11:00 ЗА ПРЕДЫДУЩИЙ день — дата по умолчанию вчера по
    # календарю объекта, не сегодня и не пустая (было пустой — искать
    # дату вручную не должно быть нужно).
    can_write = has_permission(request.state.user, "smr:write")
    return render(
        request, "form.html", "form",
        work_rows=work_rows, reason_codes=REASON_CODES,
        errors=[], warnings=warnings, ok=bool(ok),
        values={"date": object_yesterday().isoformat()},
        can_write=can_write,
    )


# ---------------------------------------------------------------------
# Гант-график производства работ — главный рабочий экран (v3.0).
# Цикл 1: только просмотр. Данные — окно дат (не весь диапазон разом,
# иначе 163 x ~160 дней тяжело рендерить и незачем гонять по сети).
# ---------------------------------------------------------------------

SOURCE_LABELS = {
    "main": "Основные работы",
    "aux": "Вспомогательные работы",
}


@app.get("/gantt")
def gantt_page(request: Request):
    can_write = has_permission(request.state.user, "smr:write")
    window_start, _window_end = get_display_window()
    return render(
        request, "gantt.html", "gantt",
        can_write=can_write, display_window_start_iso=window_start.isoformat(),
    )


@app.get("/api/gantt-metrics")
def api_gantt_metrics():
    """Компактная панель метрик для /gantt — Цикл 3. Те же функции, что
    на главной (get_evm_data/get_criticality_data), пересчитываются
    заново при каждом вызове — после правки в сетке фронт дёргает этот
    эндпоинт и обновляет панель без перезагрузки страницы."""
    evm = get_evm_data()
    crit = get_criticality_data()
    return {
        "weighted_pct": evm.get("weighted_pct") if evm.get("available") else None,
        "spi": evm.get("spi") if evm.get("available") else None,
        "cpi": evm.get("cpi") if evm.get("available") else None,
        "ppc_pct": evm.get("ppc_pct") if evm.get("available") else None,
        "ppc_promised": evm.get("ppc_promised") if evm.get("available") else None,
        "ppc_met": evm.get("ppc_met") if evm.get("available") else None,
        "forecast_date": crit["forecast_date"].isoformat() if crit.get("forecast_date") else None,
        "overdue_count": crit.get("overdue_count"),
        "required_crew": crit.get("required_crew"),
        "actual_crew": crit.get("actual_crew"),
        "deficit": crit.get("deficit"),
        "surplus": crit.get("surplus"),
        "coverage_pct": crit.get("coverage_pct"),
    }


@app.get("/api/gantt")
def api_gantt(start: str = "", days: int = 30, active_only: str = "", location: str = "", started_only: str = ""):
    active_only = bool(active_only)
    started_only = bool(started_only)
    if start:
        try:
            start_date = date_cls.fromisoformat(start)
        except ValueError:
            start_date = get_display_window()[0]
    else:
        # Правка 01.09.2026 (координатор): по умолчанию график открывался
        # с "сегодня-7", а не с начала окна отображения — 25.08 вместо
        # 01.08, при этом "сегодня-7" не была ни одной из двух дат,
        # которые координатор считал допустимыми (01.08/01.09). Теперь
        # дефолт — начало окна отображения (get_display_window()),
        # которое совпадает с directive_start после правки вопроса 12.
        # Кнопка "Сегодня" (frontend, gantt.html) намеренно не тронута —
        # у неё другой смысл (показать текущий момент), не открытие
        # страницы с нуля.
        start_date = get_display_window()[0]
    days = max(7, min(days, 90))
    end_date = start_date + timedelta(days=days - 1)

    # Ось /gantt ограничена окном отображения (решение координатора
    # 29.08.2026) — навигация "пред./след." не должна уводить за 01.08/
    # 28.11 (по умолчанию). Данные за июнь-июль в БД не трогаются,
    # просто не показываются здесь. Не ограничивает ввод факта — это
    # отдельная форма (/form, /shift), туда окно не применяется.
    window_start, window_end = get_display_window()
    if start_date < window_start:
        start_date = window_start
    end_date = start_date + timedelta(days=days - 1)
    if end_date > window_end:
        end_date = window_end
        start_date = max(window_start, end_date - timedelta(days=days - 1))

    where_extra = ""
    params = []
    if active_only:
        # Статус — вычисляется из fact_pct, не читает столбец w.status
        # (координатор, 08.09.2026, аудит целостности).
        where_extra += f" and {_work_status_expr()} not in %s"
        params.append(tuple(DONE_STATUSES))
    if location.strip():
        where_extra += " and w.location ilike %s"
        params.append(f"%{location.strip()}%")
    if started_only:
        # "Есть факт" (координатор, 31.08.2026) — вариант (в), подтверждён
        # после сверки чисел: work.fact_pct > 0 ИЛИ есть ячейка с
        # actual_crew в ТЕКУЩЕМ окне дат. Обязательно через LATEST_DP_CTE
        # (латест-wins), не сырую daily_progress — иначе фильтр мог бы
        # пометить работу "есть факт", а в самой сетке ни одной ячейки
        # с фактом не было бы видно (дедуп мог их скрыть). Работает
        # вместе с "только активные" через AND (два независимых
        # "and"-условия), не заменяет его.
        where_extra += f""" and (
            w.fact_pct > 0
            or w.id in (
                {LATEST_DP_CTE}
                select distinct work_id from latest_dp
                where date between %s and %s and actual_crew is not null
            )
        )"""
        params.append(start_date)
        params.append(end_date)

    works = query(
        f"""
        select w.id, w.code, w.name, w.unit, w.volume, w.fact_pct, {_work_status_expr()} as status,
               w.source, w.location, w.executor_type, sc.name as subcontractor_name,
               cs.current_start, cs.current_finish
        from work w
        left join subcontractor sc on sc.id = w.subcontractor_id
        left join current_schedule cs on cs.work_id = w.id
        where true {where_extra}
        order by w.source, w.code
        """,
        params,
    )
    # Просрочка — тот же список, что на /dashboard и /critical
    # (get_criticality_data, единственный источник, координатор,
    # 08.09.2026: раньше здесь был свой запрос без фильтра confidence и
    # со стухшим w.status — 31 просроченная работа против 17 канонических).
    overdue_codes = {w["code"] for w in get_criticality_data()["overdue"]}

    cells = query(
        LATEST_DP_CTE + """
        select work_id, date, planned_crew, actual_crew, reason_code, comment, source
        from latest_dp
        where date between %s and %s
        """,
        (start_date, end_date),
    )
    cell_map = {}
    for c in cells:
        cell_map.setdefault(c["work_id"], {})[c["date"].isoformat()] = {
            "p": c["planned_crew"], "a": c["actual_crew"],
            "r": c["reason_code"], "cm": _strip_source_marker(c["comment"]), "src": c["source"],
        }

    totals_rows = query(
        LATEST_DP_CTE + """
        select date, sum(planned_crew) as planned, sum(actual_crew) as actual
        from latest_dp where date between %s and %s group by date
        """,
        (start_date, end_date),
    )
    totals = {t["date"].isoformat(): {"p": t["planned"] or 0, "a": t["actual"] or 0} for t in totals_rows}

    # Правка 01.09.2026 (координатор, находка про значок ⚠ на /gantt):
    # раньше group by считал только количество по (дата, тип) и наружу
    # уходил сырой blocker_type ('id_docs') без перевода — здесь это
    # JSON-эндпоинт, Jinja-фильтр ru_blocker_type к нему не применяется
    # (это единственное место в коде, где blocker_type отдаётся клиенту
    # непереведённым — остальные 5 мест проверены, все идут через
    # шаблоны с |ru_blocker_type). Статус тоже не учитывался — снятый
    # стоп-фактор выглядел неотличимо от действующего. Теперь отдаём
    # построчно (а не count) с готовым переводом и статусом — значок
    # и подпись строятся из этого на фронте, а не пересчитывают сами.
    day_blockers_rows = query(
        "select created_at::date as d, blocker_type, status from blocker "
        "where work_id is null and created_at::date between %s and %s "
        "order by created_at",
        (start_date, end_date),
    )
    day_blockers = {}
    for r in day_blockers_rows:
        day_blockers.setdefault(r["d"].isoformat(), []).append({
            "type_ru": RU_BLOCKER_TYPE.get(r["blocker_type"], r["blocker_type"]),
            "status": r["status"],
            "status_ru": RU_BLOCKER_STATUS.get(r["status"], r["status"]),
        })

    groups = {}
    for w in works:
        g = groups.setdefault(w["source"], {"key": w["source"], "label": SOURCE_LABELS.get(w["source"], w["source"]), "works": []})
        g["works"].append({
            "id": w["id"], "code": w["code"], "name": w["name"], "unit": w["unit"],
            "volume": float(w["volume"]) if w["volume"] is not None else None,
            "fact_pct": float(w["fact_pct"]) if w["fact_pct"] is not None else None,
            "status": w["status"], "location": w["location"], "executor_type": w["executor_type"],
            "critical": w["code"] in overdue_codes,
            "subcontractor_name": w["subcontractor_name"],
            "current_start": w["current_start"].isoformat() if w["current_start"] else None,
            "current_finish": w["current_finish"].isoformat() if w["current_finish"] else None,
            "cells": cell_map.get(w["id"], {}),
        })

    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(days)]

    return {
        "start": start_date.isoformat(), "end": end_date.isoformat(), "days": days,
        "today": object_today().isoformat(),
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "dates": dates,
        "groups": list(groups.values()),
        "totals": totals,
        "day_blockers": day_blockers,
        "reason_codes": REASON_CODES,
    }


# ---------------------------------------------------------------------
# Гант-график — Цикл 2: редактирование прямо в сетке. Все мутации через
# веб-форму пишутся source='web_form' в daily_progress (тот же канал и
# то же правило конфликта, что и /form), с audit_log на каждое действие.
# ---------------------------------------------------------------------

@app.post("/api/gantt/cell")
def api_gantt_cell_save(
    request: Request,
    work_id: int = Form(...), date: str = Form(""),
    planned_crew: str = Form(""), actual_crew: str = Form(""),
    reason_code: str = Form(""), comment: str = Form(""),
):
    # Права по веткам, 30.08.2026 — график (СМР), раньше не проверялось.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "errors": ["Доступ только для группы СМР."]}, status_code=403)
    errors = []
    planned_val = validate_crew(planned_crew, "План людей", errors)
    actual_val = validate_crew(actual_crew, "Факт людей", errors)
    try:
        d = date_cls.fromisoformat(date)
    except ValueError:
        errors.append("Некорректная дата.")
        d = None
    reason_val = reason_code.strip() or None
    if reason_val and reason_val not in REASON_CODE_SET:
        errors.append("Причина простоя указана некорректно.")
    if reason_val == "OTHER" and not comment.strip():
        errors.append("При причине «Иное» комментарий обязателен.")
    comment_val = comment.strip() or None
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            """
            insert into daily_progress
                (date, work_id, planned_crew, actual_crew, reason_code, comment, source, created_by, updated_at)
            values (%s, %s, %s, %s, %s, %s, 'web_form', %s, now())
            on conflict (date, work_id, source) do update set
                planned_crew = excluded.planned_crew, actual_crew = excluded.actual_crew,
                reason_code = excluded.reason_code, comment = excluded.comment, updated_at = now()
            returning id
            """,
            (d, work_id, planned_val, actual_val, reason_val, comment_val, user_id),
        )
        dp_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'daily_progress', %s, 'gantt_cell_edit', "
            "jsonb_build_object('date', %s::text, 'work_id', %s, 'planned_crew', %s, "
            "'actual_crew', %s, 'reason_code', %s, 'comment', %s), 'правка в графике')",
            (user_id, dp_id, str(d), work_id, planned_val, actual_val, reason_val, comment_val),
        )

    run_in_transaction(_do)
    return {"ok": True}


@app.post("/api/gantt/schedule")
def api_gantt_schedule_save(
    request: Request,
    work_id: int = Form(...), current_start: str = Form(""), current_finish: str = Form(""),
):
    # Права по веткам, 30.08.2026 — график (СМР), раньше не проверялось.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "errors": ["Доступ только для группы СМР."]}, status_code=403)
    errors = []
    start_d = finish_d = None
    if current_start.strip():
        try:
            start_d = date_cls.fromisoformat(current_start.strip())
        except ValueError:
            errors.append("Некорректная дата начала.")
    if current_finish.strip():
        try:
            finish_d = date_cls.fromisoformat(current_finish.strip())
        except ValueError:
            errors.append("Некорректная дата окончания.")
    if start_d and finish_d and finish_d < start_d:
        errors.append("Дата окончания раньше даты начала.")
    if not start_d and not finish_d:
        errors.append("Укажите хотя бы одну дату.")
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        # Одна актуальная строка current_schedule на работу — не журнал версий.
        cur.execute("delete from current_schedule where work_id=%s", (work_id,))
        cur.execute(
            "insert into current_schedule (work_id, current_start, current_finish, updated_by, reason) "
            "values (%s, %s, %s, %s, %s) returning id",
            (work_id, start_d, finish_d, user_id, "изменено через график (веб)"),
        )
        cs_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'current_schedule', %s, 'gantt_schedule_edit', "
            "jsonb_build_object('work_id', %s, 'current_start', %s, 'current_finish', %s), "
            "'сдвиг сроков через график')",
            (user_id, cs_id, work_id, str(start_d) if start_d else None, str(finish_d) if finish_d else None),
        )

    run_in_transaction(_do)
    return {"ok": True}


@app.post("/api/gantt/work")
def api_gantt_new_work(
    request: Request,
    source: str = Form(""), name: str = Form(""), unit: str = Form(""), location: str = Form(""),
):
    # Права по веткам, 30.08.2026 — график (СМР), раньше не проверялось.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "errors": ["Доступ только для группы СМР."]}, status_code=403)
    errors = []
    if source not in SOURCE_LABELS:
        errors.append("Некорректный источник.")
    if not name.strip():
        errors.append("Наименование обязательно.")
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()
    prefix = {"main": "MAIN", "aux": "AUX"}[source]

    def _do(cur):
        cur.execute("select code from work where source=%s order by code desc limit 1", (source,))
        row = cur.fetchone()
        next_seq = 1
        if row:
            m = re.search(r"-(\d+)$", row["code"])
            if m:
                next_seq = int(m.group(1)) + 1
        code = f"TM35-{prefix}-{next_seq:03d}"
        cur.execute(
            "insert into work (code, source, name, unit, location, status, executor_type) "
            "values (%s, %s, %s, %s, %s, 'not_started', 'own_forces') returning id",
            (code, source, name.strip(), unit.strip() or None, location.strip() or None),
        )
        wid = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'work', %s, 'gantt_new_work', "
            "jsonb_build_object('code', %s, 'name', %s), 'добавлено через график')",
            (user_id, wid, code, name.strip()),
        )
        return code

    code = run_in_transaction(_do)
    return {"ok": True, "code": code}


@app.post("/api/gantt/subcontractor")
def api_gantt_subcontractor(request: Request, work_id: int = Form(...), name: str = Form("")):
    # Права по веткам, 30.08.2026 — график (СМР), раньше не проверялось.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "errors": ["Доступ только для группы СМР."]}, status_code=403)
    if not name.strip():
        return JSONResponse({"ok": False, "errors": ["Название субподрядчика обязательно."]}, status_code=400)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute("select id from subcontractor where name=%s", (name.strip(),))
        row = cur.fetchone()
        if row:
            sub_id = row["id"]
        else:
            cur.execute("insert into subcontractor (name) values (%s) returning id", (name.strip(),))
            sub_id = cur.fetchone()["id"]
        cur.execute(
            "update work set subcontractor_id=%s, executor_type='subcontract', updated_at=now() where id=%s",
            (sub_id, work_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'work', %s, 'gantt_assign_subcontractor', "
            "jsonb_build_object('subcontractor', %s), 'назначено через график')",
            (user_id, work_id, name.strip()),
        )

    run_in_transaction(_do)
    return {"ok": True}


# ---------------------------------------------------------------------
# Экран смены — сеточный ежедневный ввод факта (замена Excel-графика).
# Переиспользует канал web_form в daily_progress: тот же source, тот же
# ключ конфликта (date, work_id, source), что у /form и /api/gantt/cell —
# не второй параллельный механизм. В отличие от гант-модалки (одно
# сохранение сразу по всем полям строки), здесь каждая колонка
# сохраняется независимым upsert'ом, который трогает только свою
# колонку — несколько инженеров ПТО могут одновременно
# редактировать разные поля одной и той же строки, не затирая друг друга
# (см. задание: "частичное обновление, а не перезапись всей записи").
# ---------------------------------------------------------------------

SHIFT_FIELD_COLUMNS = {
    "pct": "fact_pct",
    "crew": "actual_crew",
    "reason": "reason_code",
    "comment": "comment",
}


@app.get("/shift")
def shift_page(request: Request):
    can_write = has_permission(request.state.user, "smr:write")
    return render(request, "shift.html", "shift", can_write=can_write)


@app.get("/api/shift")
def api_shift(date: str = "", all: str = "", q: str = ""):
    # Регламент: заполняем ЗА ПРЕДЫДУЩИЙ день до 11:00 текущего — дата по
    # умолчанию (без явного параметра) вчера по календарю объекта.
    try:
        d = date_cls.fromisoformat(date) if date else object_yesterday()
    except ValueError:
        d = object_yesterday()

    # ПРАВКА 29.08.2026 (требования Якименко А.И.): фильтр "статус не
    # DONE_STATUSES" (114 из 165) заменён на "есть плановое задание на
    # ЭТУ дату по графику" — прямая претензия координатора: список
    # должен быть по графику дня, не общий список активных работ.
    # ВАЖНО отличие от старого вывода 28.08 ("наличие строки в
    # daily_progress не сигнал, календарная матрица создаёт запись
    # почти на каждый день для почти каждой работы"): тот вывод был про
    # ЛЮБУЮ строку; здесь фильтр строже — именно planned_crew IS NOT
    # NULL, реальное плановое число, не просто наличие строки план/факт.
    # Проверено по данным: на конкретные даты план есть только у 5-8 из
    # 165 работ — фильтр действительно узкий, не как старый (114/165).
    # "Показать все" (весь реестр, без фильтра по графику) остаётся
    # обязательным запасным выходом.
    planned_rows = query(
        "select work_id, planned_crew from daily_progress "
        "where date=%s and source='excel_import' and planned_crew is not null",
        (d,),
    )
    planned_by_work = {r["work_id"]: r["planned_crew"] for r in planned_rows}

    where_extra = ""
    params = []
    if not all:
        if planned_by_work:
            where_extra += " and w.id = any(%s)"
            params.append(list(planned_by_work.keys()))
        else:
            # На эту дату по графику вообще ни у кого нет плана (пробел
            # импорта, см. docs/ID_KONTUR... нет — SMR-отчёт от 29.08) —
            # не запираем человека пустым списком, откатываемся к
            # прежнему критерию "не завершена физически". Статус — из
            # fact_pct, не столбец w.status (координатор, 08.09.2026).
            where_extra += f" and {_work_status_expr()} not in %s"
            params.append(tuple(DONE_STATUSES))
    if q.strip():
        where_extra += " and (w.code ilike %s or w.name ilike %s)"
        like = f"%{q.strip()}%"
        params += [like, like]

    works = query(
        f"""
        select w.id, w.code, w.name, w.location, w.source, {_work_status_expr()} as status, w.fact_pct as work_fact_pct
        from work w
        where true {where_extra}
        order by w.source, w.code
        """,
        params,
    )

    dp_rows = query(
        "select work_id, fact_pct, actual_crew, reason_code, comment, updated_at "
        "from daily_progress where date=%s and source='web_form'",
        (d,),
    )
    dp_by_work = {r["work_id"]: r for r in dp_rows}

    items = []
    for w in works:
        r = dp_by_work.get(w["id"])
        items.append({
            "id": w["id"], "code": w["code"], "name": w["name"], "location": w["location"],
            "status": w["status"],
            "work_fact_pct": float(w["work_fact_pct"]) if w["work_fact_pct"] is not None else None,
            "planned_crew": planned_by_work.get(w["id"]),
            "fact_pct": float(r["fact_pct"]) if r and r["fact_pct"] is not None else None,
            "actual_crew": r["actual_crew"] if r else None,
            "reason_code": r["reason_code"] if r else None,
            "comment": _strip_source_marker(r["comment"]) if r else None,
            "updated_at": to_object_tz(r["updated_at"]).isoformat() if r and r["updated_at"] else None,
            "filled": r is not None,
        })

    return {
        "date": d.isoformat(), "today": object_today().isoformat(),
        "all": bool(all), "q": q, "has_plan_for_date": bool(planned_by_work),
        "items": items, "reason_codes": REASON_CODES,
    }


@app.post("/api/shift/cell")
def api_shift_cell_save(
    request: Request,
    work_id: int = Form(...), date: str = Form(""), field: str = Form(...),
    value: str = Form(""),
):
    # Права по веткам, 30.08.2026 — экран смены (СМР), раньше не
    # проверялось. КРИТИЧНЫЙ путь — контур СМР работает ежедневно,
    # проверен живьём под ОБЕ группы сразу после деплоя.
    if not has_permission(request.state.user, "smr:write"):
        return JSONResponse({"ok": False, "errors": ["Доступ только для группы СМР."]}, status_code=403)
    if field not in SHIFT_FIELD_COLUMNS:
        return JSONResponse({"ok": False, "errors": ["Некорректное поле."]}, status_code=400)
    col = SHIFT_FIELD_COLUMNS[field]  # белый список — единственный способ попасть в SQL ниже

    try:
        d = date_cls.fromisoformat(date)
    except ValueError:
        return JSONResponse({"ok": False, "errors": ["Некорректная дата."]}, status_code=400)

    errors = []
    val = value.strip()
    py_val = None
    if field == "pct":
        if val:
            try:
                py_val = float(val.replace(",", "."))
            except ValueError:
                errors.append("«% готовности» должен быть числом.")
            else:
                if py_val < 0 or py_val > 100:
                    errors.append("«% готовности» должен быть от 0 до 100.")
    elif field == "crew":
        if val:
            try:
                py_val = int(val)
            except ValueError:
                errors.append("«Люди» должно быть целым числом.")
            else:
                if not (0 <= py_val <= 50):
                    errors.append("«Люди» должно быть от 0 до 50.")
    elif field == "reason":
        py_val = val or None
        if py_val and py_val not in REASON_CODE_SET:
            errors.append("Причина простоя указана некорректно.")
    elif field == "comment":
        py_val = val or None

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            f"""
            insert into daily_progress (date, work_id, {col}, source, created_by, updated_at)
            values (%s, %s, %s, 'web_form', %s, now())
            on conflict (date, work_id, source) do update set
                {col} = excluded.{col}, updated_at = now()
            returning id
            """,
            (d, work_id, py_val, user_id),
        )
        dp_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'daily_progress', %s, 'shift_cell_edit', "
            "jsonb_build_object('date', %s::text, 'work_id', %s, 'field', %s, 'value', %s::text), "
            "'экран смены')",
            (user_id, dp_id, str(d), work_id, field, py_val),
        )
        # % готовности синхронизируется с "текущим" % работы тем же
        # правилом, что уже применяет /form (используется на /dashboard,
        # /gantt, /works и т.д.) — не новое поведение, то же самое.
        if field == "pct" and py_val is not None:
            cur.execute(
                "update work set fact_pct = %s, updated_at = now() where id = %s",
                (py_val, work_id),
            )
        return dp_id

    run_in_transaction(_do)
    updated = query_one(
        "select updated_at from daily_progress where date=%s and work_id=%s and source='web_form'",
        (d, work_id),
    )
    return {"ok": True, "updated_at": to_object_tz(updated["updated_at"]).isoformat() if updated else None}


# ---------------------------------------------------------------------
# Выгрузка в CSV — задача координатора: "чтобы отказаться от Excel как
# источника, нужно дать Excel как выгрузку" (документ «Критерии
# готовности к запрету Excel», §2). Пять реестров, явно перечисленных
# там: работы, факт за период, пакеты ИД, стоп-факторы, предписания.
# UTF-8 BOM — иначе Excel на Windows показывает кириллицу битой при
# открытии CSV двойным кликом (открытие через "Данные → Импорт" не
# требуется). Разделитель — ';', не ',': Excel в русской локали иначе
# не разбивает столбцы автоматически при двойном клике.
# ---------------------------------------------------------------------

import csv
import io
import zipfile
from fastapi.responses import Response


def _csv_dmy(value):
    """Даты/время в CSV — тот же формат ДД.ММ.ГГГГ, что и в интерфейсе
    (правило проекта: только ДД.ММ.ГГГГ, никогда ISO), не сырой isoformat().
    Время (есть .hour) — по часовому поясу объекта, не как хранится в БД
    (UTC), см. to_object_tz() и решение координатора 29.08.2026."""
    if not value:
        return ""
    if hasattr(value, "hour"):
        return to_object_tz(value).strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/works.csv")
def export_works_csv(source: str = "", status: str = "", executor_type: str = "", q: str = ""):
    # Те же фильтры, что и на /works — выгружает то, что видно на экране,
    # не всегда весь реестр целиком. Статус — та же _work_status_expr(),
    # что и на /works (координатор, 08.09.2026).
    status_expr = _work_status_expr(None)
    sql = (f"select code, source, location, name, unit, {status_expr} as status, executor_type, "
           "fact_pct, plan_finish_date from work where true")
    params = []
    if source:
        sql += " and source=%s"; params.append(source)
    if status:
        sql += f" and {status_expr}=%s"; params.append(status)
    if executor_type:
        sql += " and executor_type=%s"; params.append(executor_type)
    if q:
        sql += " and name ilike %s"; params.append(f"%{q}%")
    sql += " order by code"
    rows = query(sql, params)
    out = [
        (r["code"], RU_SOURCE.get(r["source"], r["source"]), r["location"], r["name"], r["unit"],
         RU_STATUS.get(r["status"], r["status"]), RU_EXECUTOR.get(r["executor_type"], r["executor_type"]),
         r["fact_pct"], _csv_dmy(r["plan_finish_date"]))
        for r in rows
    ]
    return _csv_response(
        "works.csv",
        ["Шифр", "Источник", "Участок", "Наименование", "Ед.", "Статус", "Исполнитель", "% факт", "Плановый срок"],
        out,
    )


@app.get("/export/daily-progress.csv")
def export_daily_progress_csv(date_from: str = "", date_to: str = ""):
    try:
        d_from = date_cls.fromisoformat(date_from) if date_from else object_today() - timedelta(days=30)
    except ValueError:
        d_from = object_today() - timedelta(days=30)
    try:
        d_to = date_cls.fromisoformat(date_to) if date_to else object_today()
    except ValueError:
        d_to = object_today()
    rows = query(
        LATEST_DP_CTE + """
        select ldp.date, w.code, w.name, ldp.planned_crew, ldp.actual_crew, ldp.fact_pct,
               ldp.reason_code, ldp.comment, ldp.source, ldp.updated_at
        from latest_dp ldp join work w on w.id = ldp.work_id
        where ldp.date between %s and %s
        order by ldp.date, w.code
        """,
        (d_from, d_to),
    )
    out = [
        (_csv_dmy(r["date"]), r["code"], r["name"], r["planned_crew"], r["actual_crew"], r["fact_pct"],
         RU_REASON_CODE.get(r["reason_code"], r["reason_code"]), _strip_source_marker(r["comment"]),
         RU_DP_SOURCE.get(r["source"], r["source"]), _csv_dmy(r["updated_at"]))
        for r in rows
    ]
    return _csv_response(
        f"daily_progress_{d_from.isoformat()}_{d_to.isoformat()}.csv",
        ["Дата", "Шифр", "Наименование", "План людей", "Факт людей", "% готовности",
         "Причина простоя", "Комментарий", "Источник", "Изменено"],
        out,
    )


@app.get("/export/id-packages.csv")
def export_id_packages_csv():
    # Тот же источник и тот же набор колонок, что на странице /id-packages
    # (id_form_row/id_form_entry, не устаревший id_package) — см. ID_ROW_LIST_SQL.
    rows = query("""
        with latest as (
            select distinct on (row_id) row_id, status_id, status_date, rsk_signer_name
            from id_form_entry
            order by row_id, created_at desc
        )
        select t.label as tab_label, r.section_label,
               resp.full_name as responsible_name,
               s.label as status_label, le.status_date, le.rsk_signer_name
        from id_form_row r
        join id_form_tab t on t.id = r.tab_id
        left join id_form_responsible resp
            on resp.tab_id = t.id and resp.role = 'Ответственный за ввод данных'
        left join latest le on le.row_id = r.id
        left join id_form_status s on s.id = le.status_id
        where t.code not in ('opv', 'n')
        order by t.label, r.source_row
    """)
    out = [
        (r["tab_label"], r["section_label"], r["responsible_name"],
         r["status_label"] or "нет записи", _csv_dmy(r["status_date"]), r["rsk_signer_name"])
        for r in rows
    ]
    return _csv_response(
        "id_sections.csv",
        ["Категория", "Раздел", "Ответственный", "Статус", "Дата статуса", "Подписант РСК"],
        out,
    )


@app.get("/export/blockers.csv")
def export_blockers_csv():
    rows = query(
        "select b.*, w.code as work_code from blocker b left join work w on w.id=b.work_id "
        "order by b.created_at desc"
    )
    out = [
        (r["work_code"], RU_BLOCKER_TYPE.get(r["blocker_type"], r["blocker_type"]),
         _strip_source_marker(r["description"]), RU_BLOCKER_STATUS.get(r["status"], r["status"]),
         _csv_dmy(r["created_at"]), _csv_dmy(r["expected_resolution_date"]),
         r["responsible_name"], r["impact_days"])
        for r in rows
    ]
    return _csv_response(
        "blockers.csv",
        ["Работа", "Тип", "Описание", "Статус", "Возникло", "Ожидаемая дата снятия",
         "Ответственный", "Влияние, дней"],
        out,
    )


# /export/prescriptions.csv и вся форма /prescriptions — убраны
# 06.09.2026 (координатор: с появлением ветки РСК дублирует её
# функциональность; таблица `prescription` в БД оставлена как есть,
# была пустая, FK на неё из `blocker`/`work` не трогаю). Смотреть
# нарушения теперь — /rsk, экспорт — /export/rsk.csv.


# ---------------------------------------------------------------------
# Форма плановых сроков (`baseline_schedule`) — до 28.08.2026 формы не
# было вообще, все 146 заполненных строк попали разовым импортом
# (миграция 003_baseline_source.sql). Питает /dashboard, /today,
# /critical через confidence in ('high','medium').
# ---------------------------------------------------------------------

@app.get("/baseline")
def baseline_page(request: Request, ok: str = "", edit_id: str = "", work_id: str = ""):
    rows = query(
        "select bs.id, bs.work_id, w.id as work_pk, w.code, w.name, bs.plan_start, bs.plan_finish, bs.plan_crew, "
        "bs.confidence, bs.baseline_source, bs.comment "
        "from work w left join baseline_schedule bs on bs.work_id = w.id "
        "order by (bs.id is null), w.code"
    )
    work_rows = query("select id, code, name from work order by code")

    edit_row = None
    if edit_id.strip():
        try:
            edit_row = query_one(
                "select id, work_id, plan_start, plan_finish, plan_crew, confidence, comment "
                "from baseline_schedule where id=%s", (int(edit_id),),
            )
        except ValueError:
            edit_row = None

    values = {}
    if edit_row:
        values = {
            "work_id": edit_row["work_id"],
            "plan_start": edit_row["plan_start"].isoformat() if edit_row["plan_start"] else "",
            "plan_finish": edit_row["plan_finish"].isoformat() if edit_row["plan_finish"] else "",
            "plan_crew": edit_row["plan_crew"] if edit_row["plan_crew"] is not None else "",
            "confidence": edit_row["confidence"] or "",
            "comment": edit_row["comment"] or "",
        }
    elif work_id.strip():
        values = {"work_id": work_id.strip()}

    return render(
        request, "baseline.html", "baseline", rows=rows, work_rows=work_rows,
        errors=[], ok=bool(ok), values=values, edit_id=edit_row["id"] if edit_row else "",
    )


@app.post("/baseline")
def baseline_post(
    request: Request,
    work_id: str = Form(""),
    plan_start: str = Form(""),
    plan_finish: str = Form(""),
    plan_crew: str = Form(""),
    confidence: str = Form(""),
    comment: str = Form(""),
    edit_id: str = Form(""),
):
    # Права по веткам, 30.08.2026 — ЯВНЫЙ РАЗВОРОТ прежнего решения.
    # Ранее (перепроверка доступа, 30.08.2026, утро) здесь стояло
    # "только is_admin" — живой тест под denisov без роли admin тогда
    # показал реальную запись в чужую работу. Новое задание координатора
    # (перестройка прав на две ветки) прямо и дважды называет "плановые
    # сроки" в списке того, что должна уметь писать ВСЯ группа СМР, не
    # только координатор — проверка раздела 1.2 задания прямо этого
    # требует. Меняю на admin ИЛИ zone:smr — не тихо, фиксирую здесь.
    if not (is_admin(request.state.user) or has_permission(request.state.user, "smr:write")):
        return JSONResponse({"ok": False, "error": "Плановые сроки может менять координатор или группа СМР."}, status_code=403)

    errors = []

    work_id_val = None
    if not work_id.strip():
        errors.append("«Работа» обязательна.")
    else:
        try:
            work_id_val = int(work_id)
        except ValueError:
            errors.append("«Работа» указана некорректно.")
        else:
            if not query_one("select id from work where id=%s", (work_id_val,)):
                errors.append("Выбранная работа не найдена в справочнике.")

    start_val = None
    if plan_start.strip():
        try:
            start_val = date_cls.fromisoformat(plan_start.strip())
        except ValueError:
            errors.append("«Дата начала» указана некорректно.")

    finish_val = None
    if plan_finish.strip():
        try:
            finish_val = date_cls.fromisoformat(plan_finish.strip())
        except ValueError:
            errors.append("«Дата окончания» указана некорректно.")

    if start_val and finish_val and finish_val < start_val:
        errors.append("Дата окончания раньше даты начала.")
    if not start_val and not finish_val:
        errors.append("Укажите хотя бы одну дату (начала или окончания).")

    crew_val = validate_crew(plan_crew, "Плановая численность", errors)

    if confidence not in RU_CONFIDENCE:
        errors.append("«Уверенность» обязательна и должна быть из списка.")

    comment_val = comment.strip() or None
    edit_id_val = None
    if edit_id.strip():
        try:
            edit_id_val = int(edit_id)
        except ValueError:
            errors.append("Некорректный идентификатор редактируемой записи.")

    if errors:
        rows = query(
            "select bs.id, bs.work_id, w.id as work_pk, w.code, w.name, bs.plan_start, bs.plan_finish, bs.plan_crew, "
            "bs.confidence, bs.baseline_source, bs.comment "
            "from work w left join baseline_schedule bs on bs.work_id = w.id "
            "order by (bs.id is null), w.code"
        )
        work_rows = query("select id, code, name from work order by code")
        return render(
            request, "baseline.html", "baseline", rows=rows, work_rows=work_rows,
            errors=errors, ok=False, edit_id=edit_id,
            values={
                "work_id": work_id, "plan_start": plan_start, "plan_finish": plan_finish,
                "plan_crew": plan_crew, "confidence": confidence, "comment": comment,
            },
        )

    user_id = current_user_id_or_web_form()

    def _do(cur):
        existing_id = edit_id_val
        if not existing_id:
            cur.execute("select id from baseline_schedule where work_id=%s order by id limit 1", (work_id_val,))
            row = cur.fetchone()
            existing_id = row["id"] if row else None

        if existing_id:
            cur.execute(
                """
                update baseline_schedule set
                    plan_start=%s, plan_finish=%s, plan_crew=%s, confidence=%s, comment=%s,
                    baseline_source='web_form', approved_by=%s, approved_at=now()
                where id=%s
                """,
                (start_val, finish_val, crew_val, confidence, comment_val, user_id, existing_id),
            )
            action = "baseline_update"
        else:
            cur.execute(
                """
                insert into baseline_schedule
                    (work_id, plan_start, plan_finish, plan_crew, confidence, comment,
                     baseline_source, approved_by, approved_at)
                values (%s, %s, %s, %s, %s, %s, 'web_form', %s, now())
                returning id
                """,
                (work_id_val, start_val, finish_val, crew_val, confidence, comment_val, user_id),
            )
            existing_id = cur.fetchone()["id"]
            action = "baseline_create"

        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'baseline_schedule', %s, %s, "
            "jsonb_build_object('work_id', %s, 'plan_start', %s, 'plan_finish', %s, "
            "'plan_crew', %s, 'confidence', %s, 'comment', %s), 'форма /baseline')",
            (user_id, existing_id, action, work_id_val,
             str(start_val) if start_val else None, str(finish_val) if finish_val else None,
             crew_val, confidence, comment_val),
        )

    run_in_transaction(_do)
    return RedirectResponse(url="/baseline?ok=1", status_code=303)


# ---------------------------------------------------------------------
# "Данные" — служебный раздел (реинжиниринг v3, финал): все реестры и
# справочники, которые раньше были 12 отдельными пунктами верхнего меню.
# Ничего не спрятано — каждая карточка честно показывает, сколько в ней
# реально есть строк, включая полностью пустые реестры (subcontractor/
# material — задача #29, не заполнялись из меток Excel).
# ---------------------------------------------------------------------

@app.get("/data")
def data_hub(request: Request):
    # Числа-превью на этом хабе раньше считались СВОИМИ, упрощёнными
    # запросами — не совпадали с тем, что показывает целевая страница по
    # клику (координатор, 31.08.2026: "Критичные работы" 15 на хабе против
    # 9 на /critical; "Простои" 3 на хабе против 1 на /downtime). Правило:
    # хаб вызывает ТЕ ЖЕ функции/запросы, что и страница, не пишет свою
    # логику заново.
    crit_for_hub = get_criticality_data()
    downtime_total = query_one(
        LATEST_DP_CTE + "select count(*) as n from latest_dp where comment is not null and comment <> ''"
    )["n"]
    counts = {
        "dashboard": query_one("select count(*) as n from work")["n"],
        "critical": crit_for_hub["overdue_count"],
        "works": query_one("select count(*) as n from work")["n"],
        "resources": query_one("select count(distinct date) as n from daily_progress")["n"],
        "downtime": downtime_total,
        "subcontractors": query_one("select count(*) as n from subcontractor")["n"],
        "materials": query_one("select count(*) as n from material")["n"],
        "blockers": query_one("select count(*) as n from blocker where status='active'")["n"],
        "executor": query_one("select count(*) as n from work where executor_type='subcontract'")["n"],
        "quality": query_one(
            "select count(*) as n from work where data_quality_flag='needs_review'"
        )["n"],
        "gantt": query_one("select count(*) as n from work")["n"],
        "ssr_norms": query_one("select count(*) as n from ssr_norm")["n"],
        "norm_plan": query_one("select count(*) as n from norm_plan_item")["n"],
        "baseline": query_one("select count(*) as n from baseline_schedule")["n"],
    }
    return render(request, "data.html", "data", counts=counts)


@app.get("/healthz")
def healthz():
    query_one("select 1 as ok")
    return {"status": "ok"}
"""
Патч для main.py — добавляет:
1. Поля fact_pct и plan_finish_date в POST /form
2. Обновление существующего эндпоинта GET /api/existing-entry (возвращает fact_pct, plan_finish_date)
3. Новые эндпоинты: /id-packages, /changes, /prescriptions (GET — список, POST — добавить)
4. Jinja2-фильтр fmt_dmy для форматирования дат

Этот файл нужно вставить в main.py перед последней строкой (или в любое место после импортов).
"""

# ====== Добавить к импортам (если ещё нет) ======
from datetime import datetime as _dt, timedelta as _td

def _parse_date(s):
    """Парсит дату из строки. Возвращает date или None."""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ====== Jinja2-фильтр для дат ======
def _fmt_dmy(value):
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = _dt.fromisoformat(value.replace('Z', '+00:00')).date()
        except Exception:
            return value
    try:
        return value.strftime('%d.%m.%Y')
    except Exception:
        return str(value)

templates.env.filters['fmt_dmy'] = _fmt_dmy

# ====== Обновлённый POST /form с поддержкой fact_pct и plan_finish_date ======
# (заменяет существующий form_post)

@app.post("/form")
def form_post_v2(
    request: Request,
    work_id: str = Form(""),
    date: str = Form(""),
    planned_crew: str = Form(""),
    actual_crew: str = Form(""),
    fact_pct: str = Form(""),
    plan_finish_date: str = Form(""),
    reason_code: str = Form(""),
    comment: str = Form(""),
):
    # Права по веткам, 30.08.2026 — ввод факта (СМР), раньше не
    # проверялось. КРИТИЧНЫЙ путь — контур СМР работает ежедневно,
    # проверен живьём под ОБЕ группы сразу после деплоя. HTML-рендер
    # ошибки, не JSON — та же форма ниже так делает на все остальные
    # ошибки валидации, эта страница не fetch-форма.
    if not has_permission(request.state.user, "smr:write"):
        work_rows = query("select id, code, name from work order by code")
        return render(
            request, "form.html", "form",
            work_rows=work_rows, reason_codes=REASON_CODES,
            errors=["Доступ только для группы СМР."], warnings=[], ok=False,
            values={
                "work_id": work_id, "date": date, "planned_crew": planned_crew,
                "actual_crew": actual_crew, "fact_pct": fact_pct,
                "plan_finish_date": plan_finish_date,
                "reason_code": reason_code, "comment": comment,
            },
        )
    errors = []
    warnings = []

    work_row = None
    if not work_id.strip():
        errors.append("«Работа» обязательна.")
    else:
        try:
            work_row = query_one("select id, code, name from work where id=%s", (int(work_id),))
        except ValueError:
            errors.append("«Работа» указана некорректно.")
        if work_id.strip() and not work_row:
            errors.append("Выбранная работа не найдена в справочнике.")

    parsed_date = validate_date(date, errors, warnings)
    planned_val = validate_crew(planned_crew, "План людей", errors)
    actual_val = validate_crew(actual_crew, "Факт людей", errors)

    # Валидация fact_pct
    pct_val = None
    if fact_pct.strip():
        try:
            pct_val = float(fact_pct.replace(',', '.'))
            if pct_val < 0 or pct_val > 100:
                errors.append("«Процент выполнения» должен быть от 0 до 100.")
        except ValueError:
            errors.append("«Процент выполнения» указан некорректно.")

    # Валидация plan_finish_date
    finish_date_val = None
    if plan_finish_date.strip():
        finish_date_val = _parse_date(plan_finish_date)
        if not finish_date_val:
            errors.append("«Плановый срок окончания» указан некорректно (формат ДД.ММ.ГГГГ).")

    reason_val = reason_code.strip() or None
    if reason_val and reason_val not in REASON_CODE_SET:
        errors.append("Причина простоя указана некорректно.")
    if reason_val == "OTHER" and not comment.strip():
        errors.append("При причине «Иное» комментарий обязателен.")

    comment_val = comment.strip() or None
    if planned_val is None and actual_val is None and pct_val is None and not comment_val:
        errors.append("Заполните хотя бы одно из: план людей, факт людей, % выполнения, комментарий — пустая запись бессмысленна.")

    if errors:
        work_rows = query("select id, code, name from work order by code")
        return render(
            request, "form.html", "form",
            work_rows=work_rows, reason_codes=REASON_CODES,
            errors=errors, warnings=warnings, ok=False,
            values={
                "work_id": work_id, "date": date, "planned_crew": planned_crew,
                "actual_crew": actual_crew, "fact_pct": fact_pct,
                "plan_finish_date": plan_finish_date,
                "reason_code": reason_code, "comment": comment,
            },
        )

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            """
            insert into daily_progress
                (date, work_id, planned_crew, actual_crew, fact_pct, reason_code, comment, source, created_by, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, 'web_form', %s, now())
            on conflict (date, work_id, source) do update set
                planned_crew = excluded.planned_crew,
                actual_crew = excluded.actual_crew,
                fact_pct = excluded.fact_pct,
                reason_code = excluded.reason_code,
                comment = excluded.comment,
                updated_at = now()
            returning id
            """,
            (parsed_date, work_row["id"], planned_val, actual_val, pct_val, reason_val, comment_val, user_id),
        )
        dp_id = cur.fetchone()["id"]

        # Если указан % выполнения — обновить итоговый % по работе
        if pct_val is not None:
            cur.execute(
                "update work set fact_pct = %s, updated_at = now() where id = %s",
                (pct_val, work_row["id"]),
            )

        # Если указан плановый срок окончания — обновить
        if finish_date_val is not None:
            cur.execute(
                "update work set plan_finish_date = %s, updated_at = now() where id = %s",
                (finish_date_val, work_row["id"]),
            )

        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'daily_progress', %s, 'web_form_submit', "
            "jsonb_build_object('date', %s::text, 'work_id', %s, 'planned_crew', %s, "
            "'actual_crew', %s, 'fact_pct', %s, 'plan_finish_date', %s, "
            "'reason_code', %s, 'comment', %s), 'веб-форма v2')",
            (user_id, dp_id, str(parsed_date), work_row["id"], planned_val, actual_val,
             pct_val, str(finish_date_val) if finish_date_val else None,
             reason_val, comment_val),
        )
        return dp_id

    run_in_transaction(_do)
    q = "ok=1"
    if warnings:
        q += "&w=" + urllib.parse.quote("||".join(warnings))
    return RedirectResponse(url=f"/form?{q}", status_code=303)


# ====== Обновлённый GET /api/existing-entry (возвращает fact_pct, plan_finish_date) ======
@app.get("/api/existing-entry")
def api_existing_entry(work_id: int, date: str):
    row = query_one(
        "select dp.planned_crew, dp.actual_crew, dp.fact_pct, dp.comment, dp.reason_code, dp.updated_at, "
        "w.plan_finish_date "
        "from daily_progress dp join work w on w.id = dp.work_id "
        "where dp.work_id=%s and dp.date=%s and dp.source='web_form'",
        (work_id, date),
    )
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "planned_crew": row["planned_crew"],
        "actual_crew": row["actual_crew"],
        "fact_pct": row["fact_pct"],
        "plan_finish_date": row["plan_finish_date"].isoformat() if row["plan_finish_date"] else None,
        "comment": row["comment"],
        "reason_code": row["reason_code"],
        "updated_at": to_object_tz(row["updated_at"]).isoformat() if row["updated_at"] else None,
    }


# Единственное определение "последней записи id_form_entry по разделу" —
# раньше было переписано заново отдельно в ID_ROW_LIST_SQL, в
# id_stats_row (main.py, home_v2) и в compute_id_folder_stats()
# (последняя — вообще другой техникой, EXISTS+max(created_at)) — три
# независимых SQL-реализации одного и того же вопроса "какой сейчас
# статус у раздела" (координатор, 08.09.2026, аудит целостности; на
# момент проверки все три совпадали — 163, но это не гарантия на
# будущее без общего текста).
LATEST_ID_FORM_ENTRY_CTE = """
    with latest_id_entry as (
        select distinct on (row_id) row_id, status_id, status_date, rsk_signer_name, blocker_id
        from id_form_entry
        order by row_id, created_at desc
    )
"""

ID_ROW_LIST_SQL = LATEST_ID_FORM_ENTRY_CTE + """
    select r.id, t.label as tab_label, r.section_label,
           resp.full_name as responsible_name,
           s.label as status_label, le.status_date, le.rsk_signer_name
    from id_form_row r
    join id_form_tab t on t.id = r.tab_id
    left join id_form_responsible resp
        on resp.tab_id = t.id and resp.role = 'Ответственный за ввод данных'
    left join latest_id_entry le on le.row_id = r.id
    left join id_form_status s on s.id = le.status_id
    where t.code not in ('opv', 'n')
    order by t.label, r.source_row
"""


# ====== GET /id-packages — реестр разделов ИД (единица учёта — раздел, не пакет) ======
# Раньше читал устаревшую разовую таблицу id_package (импорт от 17.08.2026,
# 306 строк из майского xls, застывший снимок) — переведено на актуальный
# источник id_form_row/id_form_entry (координатор, 04.09.2026), тот же,
# что уже питает форму «Ввод по разделам». Колонки Участок/Состав/Сумма
# были агрегатами уровня "папки" в старой модели — здесь их нет, это
# задача будущей формы «Выполнение» (сборка папок), не этой страницы.
@app.get("/id-packages")
def id_packages_page(request: Request):
    rows = query(ID_ROW_LIST_SQL)
    total = len(rows)
    with_entry = sum(1 for r in rows if r["status_label"])
    no_entry = total - with_entry
    status_counts = {}
    for r in rows:
        if r["status_label"]:
            status_counts[r["status_label"]] = status_counts.get(r["status_label"], 0) + 1
    # Заход 3, 10.09.2026, задача 4: счётчик прикреплённых замечаний РСК —
    # чтобы попасть на экран прикрепления, не гадая id раздела руками.
    rsk_counts = {r["row_id"]: r["n"] for r in query(
        "select row_id, count(*) as n from id_row_rsk_link group by row_id"
    )}
    for r in rows:
        r["rsk_count"] = rsk_counts.get(r["id"], 0)
    return render(request, "id_packages.html", "id-packages",
                  rows=rows, total=total, with_entry=with_entry, no_entry=no_entry,
                  status_counts=status_counts)


# ====== Связь раздела ИД с замечанием РСК (заход 3, 10.09.2026, задача 4)
# Экран прикрепления/открепления. Никакого автосопоставления по тексту
# или коду раздела — только руками, по прямому указанию координатора:
# ошибочная автосвязь в документе для Заказчика хуже пустой колонки.
LATEST_RSK_ACT_ITEM_CTE = """
    with latest_act_item as (
        select distinct on (violation_id) violation_id, item_no, content
        from rsk_act_item
        order by violation_id, act_id desc
    )
"""


def compute_id_row_rsk_link_data(row_id, q=""):
    row = query_one(
        "select r.id, r.section_label, r.construction_label, t.label as tab_label "
        "from id_form_row r join id_form_tab t on t.id=r.tab_id where r.id=%s",
        (row_id,),
    )
    if not row:
        return None
    attached = query(
        LATEST_RSK_ACT_ITEM_CTE + """
        select lk.id as link_id, v.sys_no, li.content
        from id_row_rsk_link lk
        join rsk_violation v on v.id = lk.violation_id
        left join latest_act_item li on li.violation_id = v.id
        where lk.row_id = %(row_id)s
        order by v.sys_no
        """,
        {"row_id": row_id},
    )
    q_val = q.strip()
    candidates = query(
        LATEST_RSK_ACT_ITEM_CTE + """
        select v.id as violation_id, v.sys_no, li.content
        from rsk_violation v
        left join latest_act_item li on li.violation_id = v.id
        where v.is_active
          and v.id not in (select violation_id from id_row_rsk_link where row_id = %(row_id)s)
          and (%(q)s = '' or li.content ilike %(qlike)s or v.sys_no::text = %(q)s)
        order by v.sys_no desc
        limit 100
        """,
        {"row_id": row_id, "q": q_val, "qlike": f"%{q_val}%"},
    )
    label = row["section_label"] or row["construction_label"] or f"#{row['id']}"
    return {"row_id": row["id"], "label": label, "tab_label": row["tab_label"],
            "attached": attached, "candidates": candidates, "q": q_val}


@app.get("/id-rsk-link")
def id_rsk_link_page(request: Request, row_id: int, q: str = ""):
    data = compute_id_row_rsk_link_data(row_id, q)
    if not data:
        return RedirectResponse(url="/id-packages?err=" + urllib.parse.quote("Раздел не найден."), status_code=303)
    return render(request, "id_rsk_link.html", "id-packages", **data)


@app.post("/api/id-row-rsk/{row_id}/attach")
def api_id_row_rsk_attach(request: Request, row_id: int, violation_id: int = Form(...)):
    back_url = f"/id-rsk-link?row_id={row_id}"
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url=back_url + "&err=" + urllib.parse.quote("Нет доступа."), status_code=303)
    row = query_one("select id from id_form_row where id=%s", (row_id,))
    violation = query_one("select id, sys_no from rsk_violation where id=%s", (violation_id,))
    if not row or not violation:
        return RedirectResponse(url=back_url + "&err=" + urllib.parse.quote("Раздел или нарушение не найдены."), status_code=303)
    user_id = current_user_id_or_web_form()
    try:
        run_in_transaction(lambda cur: cur.execute(
            "insert into id_row_rsk_link (row_id, violation_id, created_by) values (%s, %s, %s)",
            (row_id, violation_id, user_id),
        ))
    except psycopg2.errors.UniqueViolation:
        pass  # уже прикреплено — не дублируем, не ошибка
    ok_msg = urllib.parse.quote(f"Замечание №{violation['sys_no']} прикреплено.")
    return RedirectResponse(url=f"{back_url}&ok={ok_msg}", status_code=303)


@app.post("/api/id-row-rsk-link/{link_id}/detach")
def api_id_row_rsk_detach(request: Request, link_id: int):
    link = query_one("select row_id from id_row_rsk_link where id=%s", (link_id,))
    back_url = f"/id-rsk-link?row_id={link['row_id']}" if link else "/id-packages"
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url=back_url + "&err=" + urllib.parse.quote("Нет доступа."), status_code=303)
    if not link:
        return RedirectResponse(url=back_url + "&err=" + urllib.parse.quote("Связь не найдена."), status_code=303)
    run_in_transaction(lambda cur: cur.execute("delete from id_row_rsk_link where id=%s", (link_id,)))
    return RedirectResponse(url=f"{back_url}&ok=" + urllib.parse.quote("Замечание откреплено."), status_code=303)


# ====== Экспорт "График ИД" — вид, привычный части руководства Заказчика
# (лист "График ИД" их рабочей книги "01.09.26 График ИД Хабаровск с
# комм. ред."). Разметка участок/категория/тип/исполнитель/КС2 — из
# id_row_report_meta (миграция 029, разовый импорт, см.
# tools/import_grafik_id_report_meta.py и docs/import/grafik_id_extract_20260901.csv).
# "Текущий статус раздела" — тот же канонический источник, что и на
# /id-packages (LATEST_ID_FORM_ENTRY_CTE), не отдельная копия логики.
# Разделы без отчётной разметки в этот экспорт не попадают — ожидаемо
# (импортом размечено 17 из 133 строк исходника, см.
# docs/decisions_needed_grafik_id_export.md), не считается ошибкой.

# Ночной прогон 09-10.09.2026, задача 1: янтарным (FFFFC000) красится
# весь цикл подготовки к подписи через РСК — "работа сделана, документ
# ушёл на подпись, финальной подписи нет" (та же логика, что и у
# "согласовано к подписанию"/"Подписаны сваи" из оригинала). Список — по
# факту ЖИВОГО справочника id_form_status (все 17 вкладок, проверено
# напрямую, 9 значений на вкладку): "Первичная проверка РСК",
# "Устранение замечаний 1", "Повторная проверка РСК", "Устранение
# замечаний 2". В промпте задачи упоминалось "Передано на проверку" —
# такого текста нет НИ В ОДНОЙ из 17 вкладок справочника (проверено),
# не добавлял несуществующее, см. NIGHT_RUN_20260909.md.
RSK_CYCLE_STATUS_CODES = [
    "Первичная проверка РСК",
    "Устранение замечаний 1",
    "Повторная проверка РСК",
    "Устранение замечаний 2",
]
_RSK_CYCLE_STATUSES = {s.lower() for s in RSK_CYCLE_STATUS_CODES}


def status_fill_color(status_text):
    """Легенда цвета статуса — по фактической раскраске исходного листа
    "График ИД" (разбор сделан заранее, не догадка), расширена ночным
    прогоном 09.09.2026 (задача 1) на цикл РСК из живого справочника
    статусов. Единственное место, где эта легенда описана — экспорт
    вызывает только эту функцию.

    Стопперы ("Нет проектного решения", "Замечания к площадке") —
    сознательно БЕЗ заливки: красного цвета в оригинале нет вовсе,
    вводить новый цвет в документ для Заказчика без решения ПТО не
    стал (см. NIGHT_RUN_20260909.md, "Требует решения координатора").

    Ловушка (ночной прогон, задача 2): "Подписано в карандаше" начинается
    с того же слова, что и "Подписано" — под общее правило .startswith
    подошло бы по форме и красилось бы зелёным, как подписанное. Это
    предварительное согласование БЕЗ официальной подписи — цикл РСК,
    не тир "подписано", явное исключение ДО общего правила."""
    if not status_text:
        return None
    s = status_text.strip()
    sl = s.lower()
    if sl == "подписано в карандаше":
        return "FFFFC000"
    if sl.startswith("подписано"):
        return "FF92D050"
    if sl in _RSK_CYCLE_STATUSES:
        return "FFFFC000"
    if sl in ("согласовано к подписанию", "подписаны сваи") or "на подпис" in sl:
        return "FFFFC000"
    if sl in ("да", "нет", "в работе"):
        return None
    if s == "КЭВ":
        return "FF3C1FCF"
    if s == "КРВ":
        # Оригинал красил не литеральным hex, а темой книги ("theme6") —
        # самой книги (и её XML-темы) у нас нет, воспроизвести можно
        # только приближением. Взят уже используемый на сайте синий
        # (--c-primary-light), не выдуманный с нуля. См. decisions_needed.
        return "FF2E75B6"
    return None


# Модель "группа" (часть 3, 09.09.2026) — заменяет id_row_report_meta.
# Строка Excel "Н1-4" — это ГРУППА из нескольких id_form_row (Н1, Н2,
# Н2.1, Н3, Н4), не один раздел; id_row_report_meta (row_id unique)
# технически не могла это принять — отсюда 17 из 133 в части 1. Статус
# группы теперь СЧИТАЕТСЯ по разделам-участникам (id_report_group_row),
# не читается текстом из одной записи id_form_entry. См.
# docs/decisions_needed_grafik_id_export.md, часть 3.
GRAFIK_ID_LIST_SQL = LATEST_ID_FORM_ENTRY_CTE + """
    , signed_date as (
        select e.row_id, min(e.status_date) as first_signed_date
        from id_form_entry e
        join id_form_status s on s.id = e.status_id
        where s.code = 'Подписано'
        group by e.row_id
    )
    , member_status as (
        select gr.group_id, gr.row_id,
               s.code as status_code, s.label as status_label,
               sd.first_signed_date
        from id_report_group_row gr
        left join latest_id_entry le on le.row_id = gr.row_id
        left join id_form_status s on s.id = le.status_id
        left join signed_date sd on sd.row_id = gr.row_id
    )
    select g.id as group_id, g.uchastok_no, g.uchastok_label, g.category_group,
           g.group_label, g.type_label, g.executor_name, g.display_order, g.ks2_cost_mln,
           g.source_row,
           count(ms.row_id) as n_members,
           count(*) filter (where ms.status_code = 'Подписано') as n_signed,
           max(ms.first_signed_date) as completed_date,
           mode() within group (order by ms.status_label)
               filter (where ms.status_label is not null) as mode_status_label
    from id_report_group g
    left join member_status ms on ms.group_id = g.id
    group by g.id
    order by g.uchastok_no, g.category_group, g.display_order
"""


def _grafik_group_status_text(n_members, n_signed, mode_status_label):
    """Текст статуса группы — считается по доле подписанных разделов-
    участников, не сопоставляется по тексту с одной записью (координатор,
    часть 3): 100% -> «Подписано» (совпадёт с легендой §3 части 1 как и
    100%-раздел); 0<pct<100 -> «Подписано N%» (тоже совпадёт — легенда
    красит по префиксу «Подписано», не по числу); 0% -> самый частый
    статус участников группы, а если ни у кого нет ни одной записи —
    «в работе»; группа без единого участника — явная пометка, не
    пропускается молча."""
    if n_members == 0:
        return "— (нет данных в системе)"
    if n_signed == n_members:
        return "Подписано"
    if n_signed > 0:
        pct = round(100 * n_signed / n_members)
        return f"Подписано {pct}%"
    return mode_status_label or "в работе"


def _grafik_id_rows():
    rows = query(GRAFIK_ID_LIST_SQL)
    grouped = {}
    for r in rows:
        status_text = _grafik_group_status_text(r["n_members"], r["n_signed"], r["mode_status_label"])
        completed_date = r["completed_date"] if r["n_members"] and r["n_signed"] == r["n_members"] else None
        item = dict(r)
        item["status_text"] = status_text
        item["completed_date"] = completed_date
        key = (r["uchastok_no"], r["uchastok_label"])
        grouped.setdefault(key, {})
        cat_key = r["category_group"]
        grouped[key].setdefault(cat_key, []).append(item)
    return grouped


# ====== Вкладка 2 «График ИД по папкам» — категория/участок папки не
# хранятся отдельно, выводятся из группы (id_report_group) через её
# участников (id_form_row -> id_report_group_row) -> id_folder_row.
# Папка без ни одного размеченного раздела, или с разделами,
# расходящимися по категории/участку, — пропускается, не выбираем
# произвольно (координатор, часть 2/3, 09.09.2026). ======

def _grafik_folders_by_category():
    folders = query_id_folders()
    meta_rows = query("""
        select fr.folder_id, g.category_group, g.uchastok_no, g.uchastok_label
        from id_folder_row fr
        join id_report_group_row grr on grr.row_id = fr.row_id
        join id_report_group g on g.id = grr.group_id
    """)
    combos_by_folder = {}
    for r in meta_rows:
        combos_by_folder.setdefault(r["folder_id"], set()).add(
            (r["category_group"], r["uchastok_no"], r["uchastok_label"])
        )

    labels_rows = query("""
        select fr.folder_id, rr.section_label
        from id_folder_row fr
        join id_form_row rr on rr.id = fr.row_id
        order by fr.folder_id, rr.source_row
    """)
    labels_by_folder = {}
    for r in labels_rows:
        labels_by_folder.setdefault(r["folder_id"], []).append(r["section_label"])

    grouped = {}
    skipped = []
    for f in folders:
        combos = combos_by_folder.get(f["id"])
        if not combos:
            skipped.append({"folder": f["name"], "reason": "ни один раздел папки не входит ни в одну группу разметки"})
            continue
        if len(combos) > 1:
            skipped.append({"folder": f["name"],
                             "reason": "разделы папки расходятся по категории/участку: " +
                                       "; ".join(f"{c[2]} / {c[0]}" for c in sorted(combos))})
            continue
        (category_group, uchastok_no, uchastok_label) = next(iter(combos))
        grouped.setdefault(category_group, []).append({
            "folder_id": f["id"], "name": f["name"], "folder_date": f["folder_date"],
            "sdo_transfer_date": f["sdo_transfer_date"], "amount_rub": f["amount_sum"],
            "section_labels": labels_by_folder.get(f["id"], []),
        })
    return grouped, skipped


# ====== Вкладка 3 «ИЗМЫ ПД» — реестр изменений проектной документации,
# источник — существующая таблица change. Часть колонок оригинала
# (Обозначение, Стадия, Отдел) не имеют соответствия в схеме — не
# выводятся, см. decisions_needed. ======

GRAFIK_CHANGES_SQL = """
    select code, actual_response_date, change_number, designer_name
    from change
    order by change_number nulls last, code
"""


def _grafik_changes_rows():
    return query(GRAFIK_CHANGES_SQL)


# ====== Экран решения "категория группы -> какие вкладки к ней относятся"
# (заход 4, 10.09.2026, задача 1). Причина 58 несопоставленных из 133 групп
# установлена (decisions_needed п.18): физический код (УТ1, ОПн1, КР1) может
# законно существовать как отдельный id_form_row сразу в нескольких вкладках,
# потому что вкладка — дисциплина, не физическая зона. Разрешить это может
# только человек, у которого есть основания знать, какие вкладки относятся
# к какой категории — координатора просили решить по памяти, без цифр
# перед глазами, поэтому решение не приходило. Здесь — доказательство
# (сколько кодов категории нашлось бы в каждой вкладке), не готовое
# решение: НИЧЕГО не предзаполняется по своим догадкам (см. decisions
# этого захода).
def compute_category_tab_evidence_matrix():
    idx = build_row_index(query)
    tabs = query("select id, label from id_form_tab where code not in ('opv', 'n') order by label")
    empty_groups = query("""
        select g.id, g.category_group, g.group_label
        from id_report_group g
        left join id_report_group_row gr on gr.group_id = g.id
        where gr.group_id is null
        order by g.category_group, g.id
    """)
    by_category = {}
    for g in empty_groups:
        by_category.setdefault(g["category_group"], []).append(g)

    saved = {}
    for r in query("select category_group, tab_id from id_report_category_tab"):
        saved.setdefault(r["category_group"], set()).add(r["tab_id"])

    matrix = []
    for category in sorted(by_category):
        groups = by_category[category]
        # Только токены prefix+число (range/single) — литеральные коды
        # ("У-1 (доп.)") сопоставляются по точному тексту, не по вкладке,
        # для матрицы доказательств не применимы.
        prefix_tokens = []
        for g in groups:
            for tok in resolve_tokens(tokenize_group_name(g["group_label"])):
                if tok["kind"] in ("range", "single"):
                    prefix_tokens.append(tok)

        counts = {t["id"]: 0 for t in tabs}
        for tok in prefix_tokens:
            cands = idx["by_prefix"].get(tok["prefix"], [])
            for t in tabs:
                if tok["kind"] == "range":
                    lo_i, hi_i = math.floor(tok["lo"]), math.floor(tok["hi"])
                    hit = any(tab_id == t["id"] and lo_i <= math.floor(n) <= hi_i for (n, _rid, tab_id) in cands)
                else:
                    hit = any(tab_id == t["id"] and n == tok["num"] for (n, _rid, tab_id) in cands)
                if hit:
                    counts[t["id"]] += 1

        matrix.append({
            "category": category,
            "empty_groups": len(groups),
            "counts": counts,
            "checked_tab_ids": saved.get(category, set()),
        })
    return {"tabs": tabs, "matrix": matrix}


@app.get("/id-grafik/category-mapping")
def id_grafik_category_mapping_page(request: Request):
    data = compute_category_tab_evidence_matrix()
    return render(request, "id_grafik_category_mapping.html", "id-grafik", result=None, **data)


def rerun_category_mapping(category_group, allowed_tabs):
    """Пересчёт сопоставления групп «Графика ИД» для ОДНОЙ категории, в
    границах ТОЛЬКО переданных вкладок (решение координатора: не гадать
    за пределы явно выбранного) — общая точка для веб-формы
    (api_id_grafik_category_mapping_save) и для теста идемпотентности
    (tools/test_category_mapping_rerun.py), чтобы тест проверял ровно
    тот код, что реально исполняется по кнопке "Сохранить", а не его
    копию. Идемпотентно по построению: перечитывает "пустые группы" из
    БД при каждом вызове (не из кэша/аргумента), поэтому второй вызов
    подряд с теми же аргументами видит уже заполненные группы как "не
    пустые" и не трогает их снова — та же гарантия, что уже проверена
    у tools/match_groups_v3.py."""
    empty_groups = query("""
        select g.id, g.source_row, g.group_label
        from id_report_group g
        left join id_report_group_row gr on gr.group_id = g.id
        where gr.group_id is null and g.category_group = %s
        order by g.id
    """, (category_group,))
    already_claimed = {r["row_id"] for r in query("select row_id from id_report_group_row")}
    idx = build_row_index(query)

    gained = []
    conflicts = []
    still_ambiguous = []
    for g in empty_groups:
        raw_row_ids, reasons = resolve_group_tokens(idx, g["group_label"], category_group, allowed_tabs)
        member_row_ids = set()
        for rid in raw_row_ids:
            if rid in already_claimed:
                conflicts.append(f"строка {g['source_row']} «{g['group_label']}»: раздел id={rid} уже в другой группе")
                continue
            member_row_ids.add(rid)
        if member_row_ids:
            def _apply(cur, group_id=g["id"], row_ids=member_row_ids):
                for rid in row_ids:
                    cur.execute(
                        "insert into id_report_group_row (group_id, row_id) values (%s, %s) on conflict do nothing",
                        (group_id, rid),
                    )
            run_in_transaction(_apply)
            already_claimed |= member_row_ids
            note = f"строка {g['source_row']} «{g['group_label']}»: +{len(member_row_ids)} раздел(ов)"
            if reasons:
                note += f" (частично — не все коды разобрались: {'; '.join(reasons[:2])})"
            gained.append(note)
        else:
            reason_text = "; ".join(reasons[:3]) if reasons else "коды раздела не распознаны вовсе"
            still_ambiguous.append(f"строка {g['source_row']} «{g['group_label']}»: {reason_text}")

    return {"category": category_group, "gained": gained, "still_ambiguous": still_ambiguous, "conflicts": conflicts}


@app.post("/api/id-grafik/category-mapping")
def api_id_grafik_category_mapping_save(
    request: Request, category_group: str = Form(...), tab_ids: list[int] = Form(default=[]),
):
    back_url = "/id-grafik/category-mapping"
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Нет доступа."), status_code=303)

    # Прямой рендер, не redirect — детальный список "что получилось/что
    # осталось спорным" на итог одного клика нужен целиком, не помещается
    # в query-строку redirect'а.

    user_id = current_user_id_or_web_form()

    def _save(cur):
        cur.execute("delete from id_report_category_tab where category_group=%s", (category_group,))
        for tab_id in tab_ids:
            cur.execute(
                "insert into id_report_category_tab (category_group, tab_id, created_by) values (%s, %s, %s)",
                (category_group, tab_id, user_id),
            )

    run_in_transaction(_save)

    summary = rerun_category_mapping(category_group, set(tab_ids))
    data = compute_category_tab_evidence_matrix()
    return render(request, "id_grafik_category_mapping.html", "id-grafik", result=summary, **data)


@app.get("/id-grafik")
def id_grafik_page(request: Request):
    grouped = _grafik_id_rows()
    all_groups = [r for cats in grouped.values() for v in cats.values() for r in v]
    total_groups = len(all_groups)  # всегда 133 — все строки CSV, группа создаётся безусловно
    groups_with_members = sum(1 for r in all_groups if r["n_members"] > 0)
    groups_empty = total_groups - groups_with_members
    folders_grouped, folders_skipped = _grafik_folders_by_category()
    folders_total = query_one("select count(*) as n from id_folder")["n"]
    changes_total = len(_grafik_changes_rows())
    return render(request, "id_grafik.html", "id-grafik",
                  grouped=grouped, total_groups=total_groups,
                  groups_with_members=groups_with_members, groups_empty=groups_empty,
                  folders_grouped=folders_grouped, folders_skipped=folders_skipped, folders_total=folders_total,
                  changes_total=changes_total)


# ====== Страница "График ИД" — живой прогресс по видам работ (задача 4,
# ночной прогон 09-10.09.2026). Без полного ТЗ (cc_prompt_obzor_id_grafik.md
# не дошёл — см. NIGHT_RUN_20260909.md) — реализованы блоки 1-2 из 4
# ("шесть плиток" и "Поток 15 Разделов"), по инструкции задачи блоки
# 3-4 (матрица-светофор и drill-down) не начаты, пункт меню НЕ включён
# (условие задачи: "включать, только когда первые два блока готовы" —
# они готовы, но раз вся страница ещё не полна, оставляю доступной по
# прямому URL, в меню решит добавить координатор при готовности 3-4).
#
# Гранулярность — НЕ раздел (id_form_row), а связка раздел+вид работы
# (row_id, work_type_id): один раздел может требовать до ~24 разных
# видов работ (АОСР, исполнительная схема и т.п.), у каждого свой
# статус. Отдельная, более мелкая каноническая CTE — не переиспользует
# LATEST_ID_FORM_ENTRY_CTE (та схлопывает по row_id, теряя work_type_id
# нарочно, для другой задачи — "текущий статус раздела в целом" — часть
# 3 экспорта «График ИД»). Обе CTE обслуживают разные, законные вопросы,
# путать их — значит вернуть ту самую "болезнь" из аудита 08.09.2026.
LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE = """
    with latest_by_worktype as (
        select distinct on (row_id, work_type_id) row_id, work_type_id, status_id, status_date,
               planned_rsk_date, blocker_id
        from id_form_entry
        where work_type_id is not null
        order by row_id, work_type_id, created_at desc
    )
"""


def compute_id_progress_tiles():
    """Блок 1 задачи 4 — шесть плиток. Состав плиток не был в кратком
    изложении промпта (полное ТЗ не дошло) — выбран самостоятельно как
    осмысленная сводка по связкам раздел+вид работы; если задумывался
    другой состав — заменить одним местом, см. NIGHT_RUN_20260909.md."""
    row = query_one(
        LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE
        + """
        select
            count(*) as total_pairs,
            count(distinct l.row_id) as rows_touched,
            count(*) filter (where s.code = 'Подписано') as signed,
            count(*) filter (where s.code = 'Подписано в карандаше') as pencil,
            count(*) filter (where s.code = any(%(rsk)s)) as in_rsk_cycle,
            count(*) filter (where s.code in ('Нет проектного решения', 'Замечания к площадке')) as stoppers
        from latest_by_worktype l
        join id_form_status s on s.id = l.status_id
        """,
        {"rsk": RSK_CYCLE_STATUS_CODES},
    )
    total_rows_all = query_one(
        "select count(*) as n from id_form_row r join id_form_tab t on t.id=r.tab_id "
        "where t.code not in ('opv','n')"
    )["n"]
    return {
        "total_pairs": row["total_pairs"],
        "rows_touched": row["rows_touched"],
        "total_rows_all": total_rows_all,
        "signed": row["signed"],
        "pencil": row["pencil"],
        "in_rsk_cycle": row["in_rsk_cycle"],
        "stoppers": row["stoppers"],
    }


def compute_id_progress_stream():
    """Блок 2 задачи 4 — "Поток по вкладкам": по каждой из 15 вкладок
    ИД (тот же справочник и тот же фильтр `code not in ('opv','n')`,
    что и в выпадающем списке матрицы ниже на этой же странице — раньше
    вкладка считалась через INNER JOIN от уже введённых записей, из-за
    чего вкладка без единой записи по виду работ пропадала из списка
    ВООБЩЕ, а не показывалась нулём; исправлено 10.09.2026 — LEFT JOIN
    от справочника вкладок, отсутствие записей превращается в честный
    ноль по всем трём корзинам, не в исчезновение строки). Корзины:
    зелёный (подписано), жёлтый (цикл РСК + подписано в карандаше — "в
    процессе подписания"), красный (стопперы + всё остальное, что не
    зелёное и не жёлтое)."""
    rows = query(
        LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE
        + """
        select t.id as tab_id, t.label as tab_label,
               coalesce(count(*) filter (where s.code = 'Подписано'), 0) as green_n,
               coalesce(count(*) filter (
                   where s.code = any(%(rsk)s) or s.code = 'Подписано в карандаше'
               ), 0) as yellow_n,
               coalesce(count(*) filter (
                   where s.code <> 'Подписано'
                     and s.code <> 'Подписано в карандаше'
                     and s.code <> all(%(rsk)s)
               ), 0) as red_n
        from id_form_tab t
        left join id_form_row r on r.tab_id = t.id
        left join latest_by_worktype l on l.row_id = r.id
        left join id_form_status s on s.id = l.status_id
        where t.code not in ('opv', 'n')
        group by t.id, t.label
        order by t.label
        """,
        {"rsk": RSK_CYCLE_STATUS_CODES},
    )
    return rows


# ====== Блок 3/4 задачи 4 — матрица-светофор «Раздел × Этап» и drill-down
# (продолжение прогона, 10.09.2026, cc_prompt_obzor_id_grafik.md опять не
# дошёл — реализовано по инлайн-спецификации самого промпта координатора).

ID_MATRIX_PERIOD_START = date_cls(2026, 3, 10)
ID_MATRIX_PERIOD_END = date_cls(2027, 6, 1)


# Заход 6, задача 3, 11.09.2026 — координатор явно отменил прежнюю
# разбивку на блоки 1-7/8-14/15-21/22-конец месяца: "это была моя
# придумка и она была неправильной". Колонки — настоящие календарные
# недели, понедельник-воскресенье. Каждая колонка ровно 7 дней, кроме
# первой и последней — они обрезаются по границам периода
# (ID_MATRIX_PERIOD_START/_END), если период начинается не в
# понедельник или заканчивается не в воскресенье; сам период не
# укорачивается и не растягивается до границ недели.
ID_MATRIX_DEFAULT_WINDOW = 13  # текущая неделя + 4 назад + 8 вперёд, по заданию


def _id_matrix_weeks():
    weeks = []
    monday = ID_MATRIX_PERIOD_START - timedelta(days=ID_MATRIX_PERIOD_START.weekday())
    while monday <= ID_MATRIX_PERIOD_END:
        w_end = monday + timedelta(days=6)
        p_start = max(monday, ID_MATRIX_PERIOD_START)
        p_end = min(w_end, ID_MATRIX_PERIOD_END)
        if p_start == p_end:
            label = f"{p_start:%d.%m.%Y}"
        elif p_start.month == p_end.month and p_start.year == p_end.year:
            label = f"{p_start:%d}–{p_end:%d.%m.%Y}"
        else:
            label = f"{p_start:%d.%m}–{p_end:%d.%m.%Y}"
        weeks.append({"start": p_start, "end": p_end, "label": label})
        monday += timedelta(days=7)
    return weeks


# Прецедент координатора (продолжение прогона, задача 2, 10.09.2026):
# "нет проектного решения" существует ОДНОВРЕМЕННО как статус
# id_form_status (is_stopper=true, точный текст "Нет проектного
# решения") и как отдельная, независимая причина блокировки в
# id_stop_factor/blocker (точный текст в БД — "нет проектного решения",
# нижний регистр) — сравниваем без учёта регистра. Именно эта причина —
# синяя, любая другая причина блокировки ИЛИ второй стоппер-статус
# ("Замечания к площадке") — красная. Работает одинаково что для
# раздела в целом (первый столбец), что для конкретного вида работ
# (ячейка) — обе стороны получают на вход (status_code, is_stopper,
# blocker_description) по одному и тому же протоколу.
def _id_matrix_cell_color(status_code, is_stopper, blocker_description):
    reason = None
    if blocker_description:
        reason = blocker_description
    elif is_stopper:
        reason = status_code
    if reason is not None:
        return "blue" if reason.strip().lower() == "нет проектного решения" else "red"
    if status_code == "Первичная проверка РСК":
        return "yellow"
    if status_code == "Подписано":
        return "green"
    return None


def compute_id_matrix(tab_id, window_start=None, window_size=ID_MATRIX_DEFAULT_WINDOW, show_all=False):
    """Матрица «Раздел × Этап» для одной вкладки, заход 6/задача 3,
    11.09.2026 — полная переработка семантики ячейки (координатор:
    "функция возвращает строки" не было приёмкой, приёмка — что видно
    на экране). Ячейка (раздел × неделя) — это уже не счётчик событий
    "передано на РСК" (координатор проверил живую вкладку Траншеи и
    убедился, что при таком определении сетка почти всегда пустая, даже
    там, где реальная стройка идёт), а СОСТОЯНИЕ раздела на конец этой
    недели: последняя запись id_form_entry этого раздела (по ЛЮБОМУ
    work_type_id — тот же принцип, что LATEST_ID_FORM_ENTRY_CTE
    использует для "текущего статуса раздела в целом"), датированная
    (status_date) на конец недели или раньше. Если такой записи нет —
    ячейка ПУСТАЯ (не ноль, не цвет) — "на этой неделе ещё ничего не
    было" по прямому указанию задания. Внутри непустой ячейки — вторая
    величина: сколько видов работ этого раздела уже подписаны на конец
    недели, из скольких всего (id_form_work_type вкладки).

    Полный период (ID_MATRIX_PERIOD_START..END) — обычно ~60+ недель,
    нечитаемо целиком; по умолчанию отдаётся окно ID_MATRIX_DEFAULT_WINDOW
    недель вокруг сегодняшней (4 назад + текущая + 8 вперёд), координатор
    сам назвал это "разумной отправной точкой, не обязательно точной
    цифрой". window_start/window_size — параметры окна (индексы в общем
    списке недель), show_all — показать весь период (тогда допустима
    горизонтальная прокрутка сетки — единственное разрешённое место по
    CLAUDE.md)."""
    tab = query_one("select id, code, label from id_form_tab where id=%s", (tab_id,))
    if not tab:
        return None

    rows = query(
        "select id, section_label, construction_label from id_form_row where tab_id=%s order by source_row",
        (tab_id,),
    )
    work_type_ids = [w["id"] for w in query(
        "select id from id_form_work_type where tab_id=%s", (tab_id,)
    )]
    total_work_types = len(work_type_ids)

    all_weeks = _id_matrix_weeks()
    total_weeks = len(all_weeks)
    today = object_today()
    current_idx = next((i for i, w in enumerate(all_weeks) if w["start"] <= today <= w["end"]), None)
    if current_idx is None:
        current_idx = 0 if today < all_weeks[0]["start"] else total_weeks - 1

    if show_all:
        window_start, window_end = 0, total_weeks
    else:
        max_start = max(0, total_weeks - window_size)
        if window_start is None:
            window_start = max(0, min(current_idx - 4, max_start))
        else:
            window_start = max(0, min(window_start, max_start))
        window_end = min(window_start + window_size, total_weeks)
    weeks = all_weeks[window_start:window_end]

    # История "раздел в целом" — ВСЕ записи по row_id (любой work_type_id),
    # отсортированные по (status_date, created_at) — bisect по этому
    # списку даёт "последнюю запись, датированную на дату X или раньше"
    # за O(log n), без похода в БД на каждую неделю.
    row_entries = query(
        """
        select e.row_id, e.status_date, e.created_at, s.code as status_code, s.is_stopper,
               bl.description as blocker_description
        from id_form_entry e
        join id_form_status s on s.id = e.status_id
        left join blocker bl on bl.id = e.blocker_id
        join id_form_row r on r.id = e.row_id
        where r.tab_id = %(tab_id)s
        order by e.row_id, e.status_date, e.created_at
        """,
        {"tab_id": tab_id},
    )
    row_history = defaultdict(list)
    for e in row_entries:
        row_history[e["row_id"]].append(e)
    row_dates = {rid: [e["status_date"] for e in lst] for rid, lst in row_history.items()}

    # История по (раздел, вид работы) — та же логика, но для
    # прогресс-дроби "N подписано из M" внутри ячейки.
    wt_entries = query(
        """
        select e.row_id, e.work_type_id, e.status_date, e.created_at, s.code as status_code
        from id_form_entry e
        join id_form_status s on s.id = e.status_id
        join id_form_row r on r.id = e.row_id
        where r.tab_id = %(tab_id)s and e.work_type_id is not null
        order by e.row_id, e.work_type_id, e.status_date, e.created_at
        """,
        {"tab_id": tab_id},
    )
    wt_history = defaultdict(list)
    for e in wt_entries:
        wt_history[(e["row_id"], e["work_type_id"])].append(e)
    wt_dates = {k: [e["status_date"] for e in lst] for k, lst in wt_history.items()}

    def state_asof(rid, cutoff):
        dates = row_dates.get(rid)
        if not dates:
            return None
        idx = bisect.bisect_right(dates, cutoff) - 1
        return row_history[rid][idx] if idx >= 0 else None

    def signed_count_asof(rid, cutoff):
        n = 0
        for wt_id in work_type_ids:
            dates = wt_dates.get((rid, wt_id))
            if not dates:
                continue
            idx = bisect.bisect_right(dates, cutoff) - 1
            if idx >= 0 and wt_history[(rid, wt_id)][idx]["status_code"] == "Подписано":
                n += 1
        return n

    matrix_rows = []
    for r in rows:
        label = r["section_label"] or r["construction_label"] or f"#{r['id']}"
        # "Текущий" цвет замороженного столбца — НЕ "состояние на конец
        # сегодняшней недели" (та же величина, что и любая другая ячейка),
        # а тот же принцип, что LATEST_ID_FORM_ENTRY_CTE использует везде
        # в проекте для "текущего статуса раздела": последняя ОТПРАВЛЕННАЯ
        # запись (max created_at), а не последняя по дате события — те же
        # два понятия, что и раньше в этой матрице, просто теперь явно
        # разведены по имени (current_color vs cells[].color).
        row_hist = row_history.get(r["id"])
        current_state = max(row_hist, key=lambda e: e["created_at"]) if row_hist else None
        current_color = _id_matrix_cell_color(
            current_state["status_code"] if current_state else None,
            current_state["is_stopper"] if current_state else None,
            current_state["blocker_description"] if current_state else None,
        ) if current_state else None
        cells = []
        for w in weeks:
            state = state_asof(r["id"], w["end"])
            if state is None:
                cells.append({"empty": True})
                continue
            color = _id_matrix_cell_color(state["status_code"], state["is_stopper"], state["blocker_description"])
            signed = signed_count_asof(r["id"], w["end"])
            cells.append({"empty": False, "color": color, "signed": signed, "total": total_work_types})
        matrix_rows.append({"row_id": r["id"], "label": label, "current_color": current_color, "cells": cells})

    return {
        "tab": tab,
        "weeks": weeks,
        "rows": matrix_rows,
        "window_start": window_start,
        "window_size": window_end - window_start,
        "total_weeks": total_weeks,
        "has_prev": window_start > 0,
        "has_next": window_end < total_weeks,
        "show_all": show_all,
    }


def compute_id_row_drilldown(row_id):
    """Блок 4 — разбор одного раздела на виды работ (не привязан к
    конкретному кликнутому периоду, показывает раздел целиком, как и
    сказано в задании: "the breakdown of what that раздел consists
    of"). Использует LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE — тот же
    источник "текущий статус вида работ", что и остальная страница."""
    row = query_one(
        "select r.id, r.section_label, r.construction_label, t.label as tab_label "
        "from id_form_row r join id_form_tab t on t.id=r.tab_id where r.id=%s",
        (row_id,),
    )
    if not row:
        return None
    work_types = query(
        LATEST_ID_FORM_ENTRY_BY_WORKTYPE_CTE + """
        select wt.id as work_type_id, wt.name as work_type_name,
               s.code as status_code, coalesce(s.label, 'статус не задан') as status_label,
               l.status_date, s.is_stopper, bl.description as blocker_description
        from id_form_work_type wt
        left join latest_by_worktype l on l.row_id = %(row_id)s and l.work_type_id = wt.id
        left join id_form_status s on s.id = l.status_id
        left join blocker bl on bl.id = l.blocker_id
        where wt.tab_id = (select tab_id from id_form_row where id = %(row_id)s)
        order by wt.display_order
        """,
        {"row_id": row_id},
    )
    for wt in work_types:
        wt["color"] = _id_matrix_cell_color(wt["status_code"], wt["is_stopper"], wt["blocker_description"])
        # ДД.ММ.ГГГГ здесь же, в Python, не полагаясь на Jinja-фильтр —
        # это JSON-ответ, отдаётся напрямую в JS без прохода через
        # шаблон (правило проекта: никаких ISO-дат в интерфейсе).
        wt["status_date"] = _csv_dmy(wt["status_date"]) or None
    label = row["section_label"] or row["construction_label"] or f"#{row['id']}"
    return {"row_id": row["id"], "label": label, "tab_label": row["tab_label"], "work_types": work_types}


@app.get("/api/id-progress/drilldown")
def api_id_progress_drilldown(row_id: int):
    data = compute_id_row_drilldown(row_id)
    if not data:
        return JSONResponse({"error": "Раздел не найден"}, status_code=404)
    return data


@app.get("/id-progress")
def id_progress_page(request: Request, tab_id: int = 0, week_offset: int = None, show_all: int = 0):
    tiles = compute_id_progress_tiles()
    stream = compute_id_progress_stream()
    tabs = query(
        "select id, label from id_form_tab where code not in ('opv', 'n') order by label"
    )
    selected_tab_id = tab_id or (tabs[0]["id"] if tabs else 0)
    matrix = (
        compute_id_matrix(selected_tab_id, window_start=week_offset, show_all=bool(show_all))
        if selected_tab_id else None
    )
    return render(
        request, "id_progress.html", "id-progress",
        tiles=tiles, stream=stream, tabs=tabs, selected_tab_id=selected_tab_id, matrix=matrix,
    )


# ====== Патч реального шаблона (координатор, часть 4/5, 09.09.2026) ======
# Координатор прислал настоящий файл "01.09.26 График ИД Хабаровск с
# комм. ред.xlsx" и проверенный способ его обновлять без разрушения:
# книга содержит примечания (xl/comments1.xml — 57 КБ, xl/comments2.xml
# — 121 КБ), threaded comments, VML-рисунки, printerSettings — все эти
# части ОБЫЧНЫЙ openpyxl.Workbook().save() на этом файле теряет (16
# частей архива пропадает). Поэтому лист "График ИД" больше не строится
# заново через openpyxl (как в частях 1-3) — патчится точечно поверх
# оригинала сырой правкой XML внутри .xlsx-архива (это ZIP): переписывается
# только конкретная ячейка <c>, всё остальное копируется побайтово.
# openpyxl используется только для ЧТЕНИЯ/сверки, никогда для записи
# этого файла — тот же принцип, что и в присланном PoC-скрипте.
#
# Часть 5 (правка части 4): колонки Гант-сетки (F..O) НЕ патчатся
# ВООБЩЕ. Проверка координатора на всех 133 строках показала — это не
# "дата фактического подписания", а живой ПЛАН ЗАКРЫТИЯ КС-2 по
# полумесяцам, который ведёт человек (128 из 133 строк с меткой, из
# них 109 НЕ подписаны; строка 2 — его помесячный итог на 4020 млн, то
# самое число, ради которого руководство Заказчика открывает лист).
# Патч части 4 (даже с очисткой задвоения) тихо подменял бы этот план
# нашим неполным покрытием БД по мере роста числа групп — хуже
# задвоения. Если фактическую дату подписания из системы понадобится
# показывать — через НОВУЮ колонку, которую человек добавит в шаблон
# сам (например "Факт подписания (из системы)"), не в существующую
# сетку; см. decisions_needed, часть 5, п.1 — вариант зафиксирован, не
# реализован.
#
# Статус (единственное, что патчится) — тоже не понижается: КЭВ/КРВ в
# оригинале означают "подписано, с фамилией подписанта" — тир, равный
# или выше нашего "Подписано"/"Подписано N%". Если шаблон уже на этом
# тире (или наш расчёт даёт МЕНЕЕ полное состояние, чем уже стоит) —
# ячейка не трогается вообще, расхождение уходит в лог выгрузки
# (координатор, часть 5, п.2), не в файл. Экспорт может повышать статус
# и уточнять процент, но не понижать и не заменять более информативное
# значение (с фамилией) на более общее.
#
# Самопроверка (часть 5, п.2) — не только "части архива не потерялись"
# (это не поймало бы правку Ганта в части 4), а полный инвариант: после
# патча ЛЮБАЯ ячейка листа "График ИД", кроме заявленного множества
# патченных ячеек, обязана совпасть со значением в шаблоне — сверяется
# через openpyxl (только чтение) над обеими книгами. Расхождение —
# отказ отдавать файл, откат на немодифицированный шаблон.
#
# Заявленное множество патченных ячеек — не только C{row} (статус):
# заход 3, 10.09.2026, задача 4 добавила P{row} ("Замеч. РСК,
# препятствующие принятию ИД") для групп с вручную прикреплёнными
# нарушениями РСК (id_row_rsk_link) — инвариант РАСШИРЕН явным
# добавлением координаты в тот же набор patched_coords, а не обойдён:
# любая третья, незаявленная ячейка по-прежнему ловится тем же кодом.

GRAFIK_ID_TEMPLATE_PATH = "/app/docs_import/grafik_id_template_20260901.xlsx"
GRAFIK_ID_SHEET1_PART = "xl/worksheets/sheet1.xml"  # "График ИД" — сверено через xl/_rels/workbook.xml.rels
GRAFIK_ID_SHEET1_NAME = "График ИД"


def _xlsx_read_parts(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        order = z.namelist()
        return order, {n: z.read(n) for n in order}


def _xlsx_write_parts(order, parts) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in order:
            z.writestr(n, parts[n])
    return buf.getvalue()


def _xlsx_harvest_style_by_colour(styles_xml):
    """{цвет заливки -> [индексы стилей cellXfs]} из самого шаблона —
    чтобы покрасить ячейку статуса в тот же зелёный/янтарный, что в
    оригинале, не добавляя новый стиль в styles.xml. Индексы не
    хардкодятся — пересобираются при каждом запросе (правка шаблона
    человеком может их сдвинуть)."""
    fills_block = re.search(r'<fills count="\d+">(.*?)</fills>', styles_xml, re.S).group(1)
    fills = re.findall(r"<fill>.*?</fill>|<fill/>", fills_block, re.S)

    def colour_of(fill_idx):
        f = fills[fill_idx]
        m = re.search(r'fgColor rgb="([0-9A-Fa-f]{8})"', f)
        if m:
            return m.group(1).upper()
        m = re.search(r'fgColor theme="(\d+)"', f)
        return "theme" + m.group(1) if m else None

    xfs_block = re.search(r'<cellXfs count="\d+">(.*?)</cellXfs>', styles_xml, re.S).group(1)
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


_XLSX_CELL_RE = r'<c r="%s"(?P<attrs>[^>]*?)(?:/>|>.*?</c>)'


def _xlsx_find_cell(sheet_xml, coord):
    m = re.search(_XLSX_CELL_RE % coord, sheet_xml, re.S)
    if not m:
        raise KeyError(f"ячейка {coord} отсутствует в XML листа шаблона")
    return m


def _xlsx_style_of(attrs, override=None):
    if override is not None:
        return str(override)
    m = re.search(r's="(\d+)"', attrs)
    return m.group(1) if m else None


def _xlsx_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xlsx_unescape(text):
    return (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def _xlsx_set_text(sheet_xml, coord, text, style=None):
    """inlineStr — sharedStrings.xml не трогаем вообще (иначе пришлось бы
    пересчитывать индексы всех строк книги)."""
    m = _xlsx_find_cell(sheet_xml, coord)
    s = _xlsx_style_of(m.group("attrs"), style)
    s_attr = f' s="{s}"' if s is not None else ""
    new = f'<c r="{coord}"{s_attr} t="inlineStr"><is><t>{_xlsx_escape(text)}</t></is></c>'
    return sheet_xml[: m.start()] + new + sheet_xml[m.end():]


def _xlsx_force_full_recalc(workbook_xml):
    if "fullCalcOnLoad" in workbook_xml:
        return workbook_xml
    return re.sub(r"<calcPr ([^>]*?)/>", r'<calcPr \1 fullCalcOnLoad="1"/>', workbook_xml)


def _xlsx_parse_shared_strings(sst_xml):
    items = re.findall(r"<si>(.*?)</si>", sst_xml, re.S)
    result = []
    for item in items:
        texts = re.findall(r"<t[^>]*>(.*?)</t>", item, re.S)
        result.append(_xlsx_unescape("".join(texts)))
    return result


def _xlsx_get_cell_text(sheet_xml, coord, shared_strings):
    """Текущий текст ячейки — она может быть shared-string (t="s", как в
    исходном шаблоне) или inlineStr (как после нашего патча), нужно
    уметь прочитать оба вида, чтобы сравнивать «что уже стоит» перед
    решением, повышаем мы статус или понижаем."""
    try:
        m = _xlsx_find_cell(sheet_xml, coord)
    except KeyError:
        return None
    full = m.group(0)
    attrs = m.group("attrs")
    t_attr = re.search(r't="(\w+)"', attrs)
    cell_type = t_attr.group(1) if t_attr else None
    if cell_type == "inlineStr":
        tm = re.search(r"<t[^>]*>(.*?)</t>", full, re.S)
        return _xlsx_unescape(tm.group(1)) if tm else ""
    if cell_type == "s":
        vm = re.search(r"<v>(\d+)</v>", full)
        if vm:
            idx = int(vm.group(1))
            if 0 <= idx < len(shared_strings):
                return shared_strings[idx]
        return None
    vm = re.search(r"<v>(.*?)</v>", full, re.S)
    return _xlsx_unescape(vm.group(1)) if vm else None


def _status_tier(text):
    """Тир 2 — подписано (включая КЭВ/КРВ — подписано с фамилией
    подписанта); тир 1 — любой другой известный статус (в т.ч. "Подписано
    в карандаше" — предварительное согласование, не подпись, та же
    ловушка префикса, что и в status_fill_color); тир 0 — пусто."""
    if not text:
        return 0
    s = text.strip()
    if not s:
        return 0
    if s.lower() == "подписано в карандаше":
        return 1
    if s.lower().startswith("подписано") or s in ("КЭВ", "КРВ"):
        return 2
    return 1


def _status_is_full(text):
    """Тир-2 без явного процента — трактуется как «полностью», включая
    КЭВ/КРВ (без процента по построению — это пометка подписанта, не
    доля)."""
    if not text:
        return False
    s = text.strip()
    if s.lower() == "подписано в карандаше":
        return False
    if s in ("КЭВ", "КРВ"):
        return True
    return s.lower().startswith("подписано") and "%" not in s


GRAFIK_ID_RSK_COLUMN = "P"  # "Замеч. РСК, препятствующие принятию ИД" — sharedStrings, ячейка P9


def _patch_grafik_id_sheet1(sheet_xml, styles_xml, shared_strings, groups, rsk_remarks_by_group=None):
    """Патчит колонку "Статус" (C{source_row}) для групп с n_members>0 —
    координатор, часть 5: колонки Гант-сетки не трогаются вообще (см.
    комментарий блока выше). Статус не понижается: если в шаблоне уже
    стоит состояние не менее полное, чем наш расчёт, — ячейка не
    трогается, расхождение идёт в лог.

    Заход 3, 10.09.2026, задача 4: тем же проходом патчит колонку
    "Замеч. РСК" (P{source_row}) — ТОЛЬКО когда для группы есть
    прикреплённые вручную замечания И ячейка в шаблоне сейчас пуста.
    Непустую ячейку не трогаем — это может быть человеческая пометка
    (например "Снято 17.06.26"), у нас нет способа судить, устарела она
    или нет, тот же принцип осторожности, что и у "не понижать статус".
    Возвращает (sheet_xml, {патченные координаты}, [строки лога])."""
    rsk_remarks_by_group = rsk_remarks_by_group or {}
    by_colour = _xlsx_harvest_style_by_colour(styles_xml)
    plain_style = _xlsx_style_of(_xlsx_find_cell(sheet_xml, "C26").group("attrs"))
    rsk_plain_style = _xlsx_style_of(_xlsx_find_cell(sheet_xml, f"{GRAFIK_ID_RSK_COLUMN}26").group("attrs"))

    patched = set()
    skipped = []

    for g in groups:
        if not g["n_members"] or not g["source_row"]:
            continue
        row = g["source_row"]
        coord = f"C{row}"
        try:
            _xlsx_find_cell(sheet_xml, coord)
        except KeyError:
            continue  # строка не нашлась в этом файле — не должно происходить, но не роняем весь экспорт

        template_text = _xlsx_get_cell_text(sheet_xml, coord, shared_strings)
        computed_text = g["status_text"]

        template_tier = _status_tier(template_text)
        computed_tier = _status_tier(computed_text)
        skip = False
        if template_tier > computed_tier:
            skip = True
        elif template_tier == 2 and computed_tier == 2:
            template_full = _status_is_full(template_text)
            computed_full = _status_is_full(computed_text)
            if template_full and not computed_full:
                skip = True
            elif template_full and computed_full and (template_text or "").strip() in ("КЭВ", "КРВ"):
                skip = True

        if skip:
            skipped.append(
                f"стр.{row}: шаблон {template_text!r}, система {computed_text!r} — оставлено значение шаблона"
            )
        else:
            fill_hex = status_fill_color(computed_text)
            style = by_colour.get(fill_hex, [plain_style])[0] if fill_hex else plain_style
            sheet_xml = _xlsx_set_text(sheet_xml, coord, computed_text, style=style)
            patched.add(coord)

        remarks = rsk_remarks_by_group.get(g["group_id"])
        if remarks:
            rsk_coord = f"{GRAFIK_ID_RSK_COLUMN}{row}"
            try:
                _xlsx_find_cell(sheet_xml, rsk_coord)
            except KeyError:
                continue
            rsk_template_text = (_xlsx_get_cell_text(sheet_xml, rsk_coord, shared_strings) or "").strip()
            if rsk_template_text:
                skipped.append(
                    f"стр.{row}: колонка «Замеч. РСК» уже содержит текст шаблона {rsk_template_text!r} — "
                    f"не заменена вычисленным списком {remarks!r}"
                )
                continue
            rsk_text = "; ".join(remarks)
            sheet_xml = _xlsx_set_text(sheet_xml, rsk_coord, rsk_text, style=rsk_plain_style)
            patched.add(rsk_coord)

    return sheet_xml, patched, skipped


def _xlsx_verify_only_patched_changed(template_bytes, candidate_bytes, patched_coords):
    """Полный инвариант (координатор, часть 5, п.2) — не просто "части
    архива не потерялись" (это пропустило бы патч Ганта в части 4), а
    прямое сравнение значений: на листе "График ИД" любая ячейка, кроме
    заявленных patched_coords, обязана совпасть с шаблоном. openpyxl —
    только для чтения, книги не пересохраняются."""
    from openpyxl import load_workbook
    wb_a = load_workbook(io.BytesIO(template_bytes), data_only=False)
    wb_b = load_workbook(io.BytesIO(candidate_bytes), data_only=False)
    ws_a = wb_a[GRAFIK_ID_SHEET1_NAME]
    ws_b = wb_b[GRAFIK_ID_SHEET1_NAME]
    mismatches = []
    for row in ws_a.iter_rows():
        for cell in row:
            coord = cell.coordinate
            if coord in patched_coords:
                continue
            va = cell.value
            vb = ws_b[coord].value
            if va != vb:
                mismatches.append((coord, va, vb))
    if mismatches:
        raise AssertionError(f"патч изменил незаявленные ячейки: {mismatches[:20]}")


def _grafik_group_rsk_remarks(group_ids):
    """Задача 4, заход 3: замечания РСК, прикреплённые вручную к разделам
    группы — короткая форма "№{sys_no}-{обрезанное содержание}", та же
    форма, что уже используется в живых ячейках шаблона (проверено на
    примерах "42-пробные сваи", "175-обр.засыпка" перед реализацией).
    Полный текст замечания не переносим — он на порядок длиннее, чем
    когда-либо вписывал человек в эту колонку, а домысливать "короткое
    название" вместо человека (перефразировать смысл) — риск исказить
    документ для Заказчика; берём префикс исходного текста, честно."""
    if not group_ids:
        return {}
    rows = query(
        LATEST_RSK_ACT_ITEM_CTE + """
        select grr.group_id, v.sys_no, li.content
        from id_report_group_row grr
        join id_row_rsk_link lk on lk.row_id = grr.row_id
        join rsk_violation v on v.id = lk.violation_id
        left join latest_act_item li on li.violation_id = v.id
        where grr.group_id = any(%(gids)s)
        order by grr.group_id, v.sys_no
        """,
        {"gids": list(group_ids)},
    )
    out = {}
    seen = set()
    for r in rows:
        key = (r["group_id"], r["sys_no"])
        if key in seen:
            continue
        seen.add(key)
        short = (r["content"] or "").strip()
        if len(short) > 60:
            short = short[:60].rstrip() + "…"
        text = f"{r['sys_no']}-{short}" if short else str(r["sys_no"])
        out.setdefault(r["group_id"], []).append(text)
    return out


@app.get("/export/id-grafik.xlsx")
def export_id_grafik_xlsx():
    # Заход 6, 11.09.2026, задача 1: направление поставлено на паузу
    # координатором как техдолг (KNOWN_ISSUES.md) — сам маршрут, шаблон-
    # патчер, модель групп и экран сопоставления категорий остаются в
    # коде рабочими и никуда не делись, просто выгрузка больше не отдаёт
    # файл. Раскомментировать одну эту проверку — единственное, что
    # понадобится, когда направление вернётся в работу.
    return RedirectResponse(
        url="/id-grafik?err=" + urllib.parse.quote(
            "Выгрузка временно приостановлена — направление на паузе, см. KNOWN_ISSUES.md."
        ),
        status_code=303,
    )
    grouped = _grafik_id_rows()
    all_groups = [r for cats in grouped.values() for v in cats.values() for r in v]
    rsk_remarks_by_group = _grafik_group_rsk_remarks([g["group_id"] for g in all_groups])

    with open(GRAFIK_ID_TEMPLATE_PATH, "rb") as f:
        template_bytes = f.read()
    order, parts = _xlsx_read_parts(template_bytes)

    try:
        sheet_xml = parts[GRAFIK_ID_SHEET1_PART].decode("utf-8")
        styles_xml = parts["xl/styles.xml"].decode("utf-8")
        shared_strings = _xlsx_parse_shared_strings(parts["xl/sharedStrings.xml"].decode("utf-8"))
        sheet_xml, patched_coords, skipped_log = _patch_grafik_id_sheet1(
            sheet_xml, styles_xml, shared_strings, all_groups, rsk_remarks_by_group)
        parts[GRAFIK_ID_SHEET1_PART] = sheet_xml.encode("utf-8")
        parts["xl/workbook.xml"] = _xlsx_force_full_recalc(
            parts["xl/workbook.xml"].decode("utf-8")).encode("utf-8")
        candidate_bytes = _xlsx_write_parts(order, parts)

        _xlsx_verify_only_patched_changed(template_bytes, candidate_bytes, patched_coords)

        print(f"[id-grafik.xlsx] пропатчено {len(patched_coords)} ячеек: {sorted(patched_coords)}")
        if skipped_log:
            print("[id-grafik.xlsx] статус не понижен (оставлено значение шаблона):")
            for line in skipped_log:
                print("  " + line)

        return Response(
            content=candidate_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="id_grafik.xlsx"'},
        )
    except Exception as e:
        # Ночной прогон 09.09.2026, задача 1: НЕ отдавать немодифицированный
        # шаблон молча при провале самопроверки — человек получил бы данные
        # на 01.09.2026 и не узнал бы, что выгрузка не сработала (тот же
        # класс ошибки, что разбирался в AUDIT_DATA_INTEGRITY_2026-09-08.md —
        # устаревшее число неотличимо от верного на глаз). Файл не отдаём
        # вообще, координаты расхождения — в лог.
        import traceback
        traceback.print_exc()
        print(f"[id-grafik.xlsx] ВЫГРУЗКА ОТКАЗАНА: {e!r}")
        msg = urllib.parse.quote(
            "Выгрузка не сформирована: проверка целостности не прошла, обратитесь к администратору."
        )
        return RedirectResponse(url=f"/id-grafik?err={msg}", status_code=303)


# =======================================================================
# Форма «Выполнение» — сборка папок ИД (координатор, 04.09.2026, блок D).
# Единица учёта папки — раздел id_form_row, отбираем только те, у кого
# последняя запись id_form_entry имеет статус «Подписано» (тот же паттерн
# distinct-on, что и в ID_ROW_LIST_SQL выше). Один раздел — не больше чем
# в одной папке: обеспечено UNIQUE(row_id) в id_folder_row на уровне БД,
# а не только проверкой в коде — прямая вставка дубля упадёт сама.
# Ограничения на число разделов в папке нет вообще (отменено координатором
# 04.09.2026 — исторические папки заносятся задним числом произвольного
# размера, подсказка "рекомендовано 10-100" только сбивала с толку).
# Сумма — одна на папку целиком (не по
# разделам, решение координатора 03.09.2026), хранится в id_folder.amount_rub;
# id_folder_row суммы не хранит вовсе — стоимость раздела не нужна.
# Номер папки всегда автоматический, формат "ТМ-000" по id папки.
# =======================================================================

ID_FOLDER_CONTRACT_TOTAL = 4078191380.98  # Всего с НДС по контракту — координатор, докс-ТЗ

ID_AVAILABLE_ROWS_SQL = """
    with latest as (
        select distinct on (row_id) row_id, status_id
        from id_form_entry
        order by row_id, created_at desc
    )
    select r.id, t.label as tab_label, r.section_label
    from id_form_row r
    join id_form_tab t on t.id = r.tab_id
    join latest le on le.row_id = r.id
    join id_form_status s on s.id = le.status_id and s.code = 'Подписано'
    where t.code not in ('opv', 'n')
      and not exists (select 1 from id_folder_row fr where fr.row_id = r.id)
    order by t.label, r.source_row
"""


# Ночной прогон 09-10.09.2026, задача 3 — «Обзор ИД» вокруг ПАПКИ, не
# раздела: деньги платят за ПОДПИСАННУЮ папку, а модель до сих пор не
# различала "передана в СДО" (отправлена на проверку) и "подписана"
# (принята и подписана) — signed_folders_sum считался по
# sdo_transfer_date, то есть фактически по дате отправки, не приёмки.
#
# Стадия папки НЕ хранится отдельным полем (та же дисциплина, что весь
# проект — не дублировать вычислимое состояние в колонке, которая может
# разойтись с фактом, см. AUDIT_DATA_INTEGRITY_2026-09-08.md) —
# вычисляется здесь, в ЕДИНСТВЕННОМ месте, по тому, какая из дат
# заполнена последней в цепочке. Каждая папка — ровно одна стадия
# (нужно для воронки: сумма по стадиям обязана сходиться с count(*)).
ID_FOLDER_STAGES = ["formed", "transferred", "checking", "signed", "ks2"]
ID_FOLDER_STAGE_LABELS = {
    "formed": "Сформирована",
    "transferred": "Передана в СДО",
    "checking": "Проверка",
    "signed": "Подписана",
    "ks2": "КС-2",
}


def id_folder_stage(folder):
    """Единственное место, определяющее стадию папки — Сформирована →
    Передана в СДО → Проверка → Подписана → КС-2. folder — dict/Row с
    ключами sdo_transfer_date/check_start_date/signed_date/ks2_date."""
    if folder.get("ks2_date"):
        return "ks2"
    if folder.get("signed_date"):
        return "signed"
    if folder.get("check_start_date"):
        return "checking"
    if folder.get("sdo_transfer_date"):
        return "transferred"
    return "formed"


def query_id_folders(order="desc"):
    """Список всех папок с числом разделов — общий источник для
    /id-folders («Выполнение») и /id-folders/registry («Реестр папок»),
    чтобы не разойтись в двух вариантах одного и того же SQL."""
    direction = "asc" if order == "asc" else "desc"
    return query(f"""
        select f.id, f.name, f.folder_date, f.sdo_transfer_date, f.sdo_signer_name,
               f.check_start_date, f.signed_date, f.signed_by, f.ks2_date, f.ks2_no,
               f.amount_rub as amount_sum, f.amount_smeta_rub, count(fr.id) as row_count
        from id_folder f
        left join id_folder_row fr on fr.folder_id = f.id
        group by f.id
        order by f.id {direction}
    """)


def compute_id_folder_funnel():
    """Воронка папок по 5 стадиям — количество и сумма (только там, где
    она известна: amount_smeta_rub вводится с момента подписания, для
    более ранних стадий его ещё нет). Сумма по count(*) стадий обязана
    сходиться с count(*) from id_folder — каждая папка ровно в одной
    стадии, стадии не пересекаются."""
    folders = query("select id, sdo_transfer_date, check_start_date, signed_date, ks2_date, "
                     "amount_smeta_rub from id_folder")
    funnel = {s: {"count": 0, "sum": 0.0, "known_sum_count": 0} for s in ID_FOLDER_STAGES}
    for f in folders:
        stage = id_folder_stage(f)
        funnel[stage]["count"] += 1
        if f["amount_smeta_rub"] is not None:
            funnel[stage]["sum"] += float(f["amount_smeta_rub"])
            funnel[stage]["known_sum_count"] += 1
    return funnel


def compute_id_folder_transitions(limit=10):
    """"Переходы" — последние по времени смены стадии по всем папкам
    (строка 4 дашборда папок, задача 3). Каждая папка может дать до 4
    переходов (передана/проверка/подписана/КС-2) — берём все, сортируем
    по дате, показываем последние `limit`."""
    folders = query(
        "select id, name, sdo_transfer_date, check_start_date, signed_date, ks2_date "
        "from id_folder"
    )
    events = []
    field_labels = [
        ("sdo_transfer_date", "Передана в СДО"),
        ("check_start_date", "Начата проверка"),
        ("signed_date", "Подписана"),
        ("ks2_date", "Оформлен КС-2"),
    ]
    for f in folders:
        for field, label in field_labels:
            if f[field]:
                events.append({"folder_name": f["name"], "folder_id": f["id"],
                                "label": label, "date": f[field]})
    events.sort(key=lambda e: e["date"], reverse=True)
    return events[:limit]


def compute_id_folder_stats():
    """Общая сводка для /id-folders и плитки дашборда — один источник цифр,
    не считать дважды в двух местах по-разному."""
    total_rows = query_one(
        "select count(*) as n from id_form_row r join id_form_tab t on t.id=r.tab_id "
        "where t.code not in ('opv','n')"
    )["n"]
    signed_total = query_one(
        LATEST_ID_FORM_ENTRY_CTE +
        "select count(*) as n from id_form_row r join id_form_tab t on t.id=r.tab_id "
        "join latest_id_entry le on le.row_id=r.id "
        "join id_form_status s on s.id=le.status_id "
        "where t.code not in ('opv','n') and s.code='Подписано'"
    )["n"]
    # "Остаток неподписанных разделов" (координатор, докс D5) — буквально
    # разделы, которые ещё не в статусе «Подписано», а не «подписанные,
    # но ещё не в папке» (для второго смысла ниже отдельная переменная,
    # не одно и то же — не путать).
    unsigned_count = total_rows - signed_total

    in_folder_total = query_one("select count(*) as n from id_folder_row")["n"]
    signed_not_in_folder = signed_total - in_folder_total if signed_total >= in_folder_total else 0

    folders_count = query_one("select count(*) as n from id_folder")["n"]

    # Денежный источник истины — amount_smeta_rub ПОДПИСАННЫХ (signed_date
    # заполнена) папок, не amount_rub (прикидка ПТО при сборке) и не по
    # факту передачи в СДО (задача 3, ночной прогон 09-10.09.2026).
    # Старое значение (по sdo_transfer_date/amount_rub) считается тоже —
    # только для сравнения старое/новое в отчёте прогона, в денежных
    # показателях интерфейса больше не участвует.
    signed_folders_sum = query_one(
        "select coalesce(sum(amount_smeta_rub), 0) as s from id_folder where signed_date is not null"
    )["s"]
    signed_folders_sum_old_by_transfer_estimate = query_one(
        "select coalesce(sum(amount_rub), 0) as s from id_folder where sdo_transfer_date is not null"
    )["s"]
    manual_sum = query_one("select coalesce(sum(amount_rub), 0) as s from id_manual_volume")["s"]
    # Координатор, 08.09.2026: "Остаток в деньгах" не вычитал незакрытый
    # ручной объём — считал только minus подписанные папки, показывал
    # фактически "контракт минус подписано", не настоящий остаток.
    money_remaining = ID_FOLDER_CONTRACT_TOTAL - float(signed_folders_sum) - float(manual_sum)

    # Продолжение 10.09.2026: "Подписано, ₽" упало до 0,00 — честно, но
    # читатель не может отличить "ещё ничего не подписано" от "данные не
    # внесены". Две плитки-счётчика без текста-пояснения (запрещено
    # правилом "заголовок — и сразу содержимое"), делают эту разницу
    # видимой: сколько папок ждёт ввода сметной стоимости (подписаны, но
    # amount_smeta_rub ещё пуст) и сколько ещё вообще не подписано.
    awaiting_smeta_count = query_one(
        "select count(*) as n from id_folder where signed_date is not null and amount_smeta_rub is null"
    )["n"]
    not_signed_count = query_one(
        "select count(*) as n from id_folder where signed_date is null"
    )["n"]

    return {
        "total_rows": total_rows, "signed_total": signed_total, "unsigned_count": unsigned_count,
        "signed_not_in_folder": signed_not_in_folder, "folders_count": folders_count,
        "signed_folders_sum": signed_folders_sum, "money_remaining": money_remaining,
        "contract_total": ID_FOLDER_CONTRACT_TOTAL, "manual_sum": manual_sum,
        "signed_folders_sum_old_by_transfer_estimate": signed_folders_sum_old_by_transfer_estimate,
        "funnel": compute_id_folder_funnel(),
        "awaiting_smeta_count": awaiting_smeta_count, "not_signed_count": not_signed_count,
    }


@app.get("/id-folders")
def id_folders_page(request: Request):
    folders = query_id_folders()
    for f in folders:
        f["stage"] = id_folder_stage(f)
        f["stage_label"] = ID_FOLDER_STAGE_LABELS[f["stage"]]
    stats = compute_id_folder_stats()
    manual_volumes = query("select id, description, amount_rub, created_at from id_manual_volume order by id desc")
    transitions = compute_id_folder_transitions()
    # "Активных ИЗМ (ДПР)" — строка 3 воронки (задача 3, ночной прогон):
    # тот же признак "активная" (не завершена/не архивна), что и на
    # /changes и в change_stats_row (home_v2) — не отдельное правило.
    active_changes = query_one(
        "select count(*) as n from change where status not in ('INCLUDED_IN_RD', 'ARCHIVED')"
    )["n"]

    return render(request, "id_folders.html", "id-folders",
                  folders=folders, stats=stats, manual_volumes=manual_volumes,
                  transitions=transitions, active_changes=active_changes,
                  stage_labels=ID_FOLDER_STAGE_LABELS, stages=ID_FOLDER_STAGES)


@app.get("/id-folders/registry")
def id_folders_registry_page(request: Request, status: str = "all", sort: str = "desc"):
    folders = query_id_folders(order=sort)
    for f in folders:
        f["stage"] = id_folder_stage(f)
        f["stage_label"] = ID_FOLDER_STAGE_LABELS[f["stage"]]

    totals = {
        "folders_count": len(folders),
        "transferred_count": sum(1 for f in folders if f["sdo_transfer_date"]),
        "amount_total": sum(float(f["amount_sum"] or 0) for f in folders),
        # Та же сумма, что signed_folders_sum в compute_id_folder_stats()
        # (координатор, 08.09.2026) — раньше считалась второй раз, в
        # Python. С задачи 3 (ночной прогон 09-10.09.2026) это сумма
        # amount_smeta_rub ДЕЙСТВИТЕЛЬНО подписанных папок (signed_date),
        # не оценка по факту передачи в СДО — ключ и подпись в шаблоне
        # переименованы вместе с расчётом, чтобы название не разошлось со
        # смыслом.
        "amount_signed": float(compute_id_folder_stats()["signed_folders_sum"]),
    }

    if status == "formed":
        folders = [f for f in folders if not f["sdo_transfer_date"]]
    elif status == "transferred":
        folders = [f for f in folders if f["sdo_transfer_date"]]
    elif status == "awaiting_smeta":
        folders = [f for f in folders if f["signed_date"] and not f["amount_smeta_rub"]]
    elif status == "not_signed":
        folders = [f for f in folders if not f["signed_date"]]

    return render(request, "id_folders_registry.html", "id-folders-registry",
                  folders=folders, totals=totals, status=status, sort=sort)


@app.get("/export/id-folders.csv")
def export_id_folders_csv(status: str = "all"):
    folders = query_id_folders()
    if status == "formed":
        folders = [f for f in folders if not f["sdo_transfer_date"]]
    elif status == "transferred":
        folders = [f for f in folders if f["sdo_transfer_date"]]
    out = [
        (f["name"], _csv_dmy(f["folder_date"]), f["row_count"], f["amount_sum"] or 0,
         f["sdo_signer_name"] or "", _csv_dmy(f["sdo_transfer_date"]),
         ID_FOLDER_STAGE_LABELS[id_folder_stage(f)], f["amount_smeta_rub"] or "")
        for f in folders
    ]
    return _csv_response(
        "id_folders.csv",
        ["Номер папки", "Дата формирования", "Разделов", "Стоимость (оценка), ₽",
         "Подписант реестра передачи", "Дата передачи в СДО", "Стадия", "Сметная стоимость, ₽"],
        out,
    )


@app.get("/id-folders/new")
def id_folder_new_page(request: Request):
    available = query(ID_AVAILABLE_ROWS_SQL)
    available_by_tab = {}
    for r in available:
        available_by_tab.setdefault(r["tab_label"], []).append(r)
    return render(request, "id_folder_new.html", "id-folders-new",
                  available_by_tab=available_by_tab)


@app.post("/api/id-folder")
def api_id_folder_create(request: Request, folder_date: str = Form(...), row_ids: list[int] = Form(default=[])):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    date_val = _parse_date(folder_date)
    if not date_val:
        return RedirectResponse(
            url="/id-folders/new?err=" + urllib.parse.quote("Дата создания папки указана некорректно."),
            status_code=303,
        )
    if not row_ids:
        return RedirectResponse(
            url="/id-folders/new?err=" + urllib.parse.quote("Выберите хотя бы один раздел."),
            status_code=303,
        )

    user_id = current_user_id_or_web_form()

    def _do(cur):
        # Заход 6, 11.09.2026, задача 2: номер папки — независимая метка,
        # не производная от id_folder.id. Раньше номер брался прямо из
        # только что сгенерированного id (f"ТМ-{folder_id:03d}") — это
        # ровно то смешение "метка = идентификатор", которое ломается
        # при любой перенумерации (перенумеровка меняет name, id не
        # трогает; следующая папка после неё получила бы дыру или
        # столкновение, если бы номер по-прежнему брался из id). Теперь
        # номер — max(текущих реальных "ТМ-NNN") + 1, читается в той же
        # транзакции, что и вставка, чтобы не разъехаться при двух
        # одновременных сохранениях.
        cur.execute(r"select name from id_folder where name ~ '^ТМ-\d+$'")
        existing_nums = [int(r["name"].split("-", 1)[1]) for r in cur.fetchall()]
        next_num = (max(existing_nums) if existing_nums else 0) + 1
        folder_name = f"ТМ-{next_num:03d}"

        cur.execute(
            "insert into id_folder (name, folder_date, created_by) values (%s, %s, %s) returning id",
            (folder_name, date_val, user_id),
        )
        folder_id = cur.fetchone()["id"]
        errors = []
        for row_id in row_ids:
            cur.execute("select id from id_folder_row where row_id=%s", (row_id,))
            if cur.fetchone():
                errors.append(f"Раздел #{row_id} уже в другой папке — пропущен.")
                continue
            cur.execute(
                "insert into id_folder_row (folder_id, row_id) values (%s, %s)",
                (folder_id, row_id),
            )
        return folder_id, folder_name, errors
    folder_id, folder_name, errors = run_in_transaction(_do)
    ok_msg = urllib.parse.quote(f"Папка «{folder_name}» сформирована.")
    return RedirectResponse(
        url=f"/id-folders/{folder_id}?ok={ok_msg}" + ("&warn=1" if errors else ""), status_code=303
    )


# Заход 3, 10.09.2026, задача 1: массовый ввод денег по папкам ИД —
# ОБЯЗАН стоять раньше "/id-folders/{folder_id}" ниже, иначе Starlette
# матчит первый зарегистрированный шаблон, "bulk-entry" пытается
# распарситься как int folder_id и падает 422 (найдено и исправлено
# в этой же сессии при первой проверке).
@app.get("/id-folders/bulk-entry")
def id_folders_bulk_entry_page(request: Request):
    folders = query_id_folders(order="asc")
    return render(request, "id_folders_bulk_entry.html", "id-folders", folders=folders, rsk_signers=RSK_SIGNERS)


@app.get("/id-folders/{folder_id}")
def id_folder_detail_page(request: Request, folder_id: int):
    folder = query_one("select * from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders", status_code=303)
    rows_in_folder = query(
        "select fr.id as folder_row_id, r.section_label, t.label as tab_label "
        "from id_folder_row fr join id_form_row r on r.id=fr.row_id join id_form_tab t on t.id=r.tab_id "
        "where fr.folder_id=%s order by t.label, r.source_row",
        (folder_id,),
    )
    available = query(ID_AVAILABLE_ROWS_SQL)
    available_by_tab = {}
    for r in available:
        available_by_tab.setdefault(r["tab_label"], []).append(r)
    return render(request, "id_folder_detail.html", "id-folders",
                  folder=folder, rows_in_folder=rows_in_folder,
                  available_by_tab=available_by_tab, rsk_signers=RSK_SIGNERS,
                  stage=id_folder_stage(folder), stage_label=ID_FOLDER_STAGE_LABELS[id_folder_stage(folder)])


@app.post("/api/id-folder/{folder_id}/rows")
def api_id_folder_add_rows(request: Request, folder_id: int, row_ids: list[int] = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    errors = []

    def _do(cur):
        for row_id in row_ids:
            cur.execute("select id from id_folder_row where row_id=%s", (row_id,))
            if cur.fetchone():
                errors.append(f"Раздел #{row_id} уже в другой папке — пропущен.")
                continue
            cur.execute(
                "insert into id_folder_row (folder_id, row_id) values (%s, %s)",
                (folder_id, row_id),
            )
    run_in_transaction(_do)
    ok_msg = urllib.parse.quote("Раздел(ы) добавлены в папку.")
    return RedirectResponse(
        url=f"/id-folders/{folder_id}?ok={ok_msg}" + ("&warn=1" if errors else ""), status_code=303
    )


@app.post("/api/id-folder-row/{folder_row_id}/remove")
def api_id_folder_row_remove(request: Request, folder_row_id: int):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    row = query_one("select folder_id from id_folder_row where id=%s", (folder_row_id,))
    if not row:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Запись не найдена."), status_code=303)
    run_in_transaction(lambda cur: cur.execute("delete from id_folder_row where id=%s", (folder_row_id,)))
    ok_msg = urllib.parse.quote("Раздел убран из папки.")
    return RedirectResponse(url=f"/id-folders/{row['folder_id']}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/amount")
def api_id_folder_amount(request: Request, folder_id: int, amount_rub: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    try:
        amt = float(amount_rub.replace(",", "."))
    except ValueError:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Сумма указана некорректно."), status_code=303)
    if amt <= 0:
        return RedirectResponse(
            url=back_url + "?err=" + urllib.parse.quote("Сумма папки должна быть больше нуля."),
            status_code=303,
        )
    run_in_transaction(
        lambda cur: cur.execute("update id_folder set amount_rub=%s where id=%s", (amt, folder_id))
    )
    # Была точка вместо запятой в этом флеш-сообщении — тот же паттерн,
    # что уже правился в шаблонах (координатор, 08.09.2026), просто не
    # в Jinja-фильтре, а в Python-строке; заодно поймал по пути.
    ok_msg = urllib.parse.quote(f"Сумма папки сохранена: {_ru_money(amt)} ₽.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/date")
def api_id_folder_date(request: Request, folder_id: int, folder_date: str = Form(...)):
    # Доступно и после передачи в СДО (координатор, 08.09.2026) — дата
    # создания папки не участвует ни в одном расчёте (проверено: только
    # отображение в /id-folders, /id-folders/registry, /export/
    # id-folders.csv и подписи на этой странице — все читают живьём из
    # id_folder.folder_date на каждый запрос, ничего не кэширует и не
    # пересчитывает от неё производных значений), поэтому запрет на
    # правку после передачи был бы искусственным ограничением, не
    # диктуемым логикой.
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    date_val = _parse_date(folder_date)
    if not date_val:
        return RedirectResponse(
            url=back_url + "?err=" + urllib.parse.quote("Дата создания указана некорректно."),
            status_code=303,
        )
    run_in_transaction(
        lambda cur: cur.execute("update id_folder set folder_date=%s where id=%s", (date_val, folder_id))
    )
    ok_msg = urllib.parse.quote(f"Дата создания папки сохранена: {_dmy(date_val)}.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/sdo")
def api_id_folder_sdo(request: Request, folder_id: int,
                       sdo_transfer_date: str = Form(...), sdo_signer_name: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    date_val = _parse_date(sdo_transfer_date)
    if not date_val:
        return RedirectResponse(
            url=back_url + "?err=" + urllib.parse.quote("Дата передачи в СДО указана некорректно."), status_code=303
        )
    if sdo_signer_name not in RSK_SIGNERS:
        return RedirectResponse(
            url=back_url + "?err=" + urllib.parse.quote("Недопустимый подписант реестра передачи."), status_code=303
        )
    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set sdo_transfer_date=%s, sdo_signer_name=%s where id=%s",
        (date_val, sdo_signer_name, folder_id),
    ))
    # Редирект в реестр («Смотреть»), не назад на карточку папки (решение
    # координатора 04.09.2026) — так видно и подтверждение, и результат
    # в общем списке одним действием.
    ok_msg = urllib.parse.quote(f"Папка «{folder['name']}» передана в СДО {_csv_dmy(date_val)}.")
    return RedirectResponse(url=f"/id-folders/registry?ok={ok_msg}", status_code=303)


# ── Ночной прогон 09-10.09.2026, задача 3: переходы по стадиям папки
# после "Передана в СДО" — Проверка → Подписана → КС-2. Даты
# принимаются и задним числом, тот же принцип, что и у /sdo выше —
# для папок, собираемых по уже прошедшим стадии разделам с начала
# стройки. Строгий порядок стадий НЕ проверяется (можно проставить
# дату подписания раньше, чем дату начала проверки, если так было в
# жизни) — форма честно отражает, что человек ввёл, а не навязывает
# последовательность, которой сама папка могла не следовать. ──

@app.post("/api/id-folder/{folder_id}/check-start")
def api_id_folder_check_start(request: Request, folder_id: int, check_start_date: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    date_val = _parse_date(check_start_date)
    if not date_val:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Дата начала проверки указана некорректно."), status_code=303)
    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set check_start_date=%s where id=%s", (date_val, folder_id),
    ))
    ok_msg = urllib.parse.quote(f"Проверка папки «{folder['name']}» начата {_csv_dmy(date_val)}.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/sign")
def api_id_folder_sign(request: Request, folder_id: int,
                        signed_date: str = Form(...), signed_by: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    date_val = _parse_date(signed_date)
    if not date_val:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Дата подписания указана некорректно."), status_code=303)
    # Таблица решений ночного прогона: подписант папки — тот же список,
    # что и подписант реестра передачи (RSK_SIGNERS).
    if signed_by not in RSK_SIGNERS:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Недопустимый подписант."), status_code=303)
    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set signed_date=%s, signed_by=%s where id=%s", (date_val, signed_by, folder_id),
    ))
    ok_msg = urllib.parse.quote(f"Папка «{folder['name']}» подписана {_csv_dmy(date_val)}.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/ks2")
def api_id_folder_ks2(request: Request, folder_id: int,
                       ks2_date: str = Form(...), ks2_no: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    date_val = _parse_date(ks2_date)
    if not date_val:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Дата КС-2 указана некорректно."), status_code=303)
    if not ks2_no.strip():
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Номер КС-2 обязателен."), status_code=303)
    # Таблица решений ночного прогона: КС-2 в контуре ИД — только дата и
    # номер на папке, связей с другими сущностями (акты КС-2 подрядчика
    # и т.п.) не заводим.
    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set ks2_date=%s, ks2_no=%s where id=%s", (date_val, ks2_no.strip(), folder_id),
    ))
    ok_msg = urllib.parse.quote(f"КС-2 №{ks2_no.strip()} по папке «{folder['name']}» оформлен {_csv_dmy(date_val)}.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/id-folder/{folder_id}/smeta")
def api_id_folder_smeta(request: Request, folder_id: int, amount_smeta_rub: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name, signed_date from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)
    back_url = f"/id-folders/{folder_id}"
    # "Доступен с момента подписания" (задача 3) — не только скрыт в
    # форме, проверяется и на сервере, иначе прямой POST в обход формы
    # мог бы занести сметную стоимость до подписания.
    if not folder["signed_date"]:
        return RedirectResponse(
            url=back_url + "?err=" + urllib.parse.quote("Сметная стоимость вводится только после подписания папки."),
            status_code=303,
        )
    try:
        amt = float(amount_smeta_rub.replace(",", "."))
    except ValueError:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Сметная стоимость указана некорректно."), status_code=303)
    if amt <= 0:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Сметная стоимость должна быть больше нуля."), status_code=303)
    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set amount_smeta_rub=%s where id=%s", (amt, folder_id),
    ))
    ok_msg = urllib.parse.quote(f"Сметная стоимость папки «{folder['name']}» сохранена: {_ru_money(amt)} ₽.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


# ====== Массовый ввод денег по папкам ИД (заход 3, 10.09.2026, задача 1) ======
# Одна карточка за раз — это трение, из-за которого дашборд остаётся
# пустым (0,00 ₽ подписано при 9 реальных папках). Одна страница,
# редактируемая построчно, обычными POST-формами (form="row-N" на
# полях — не вложенный <form> внутри <tr>, невалидный HTML5) — те же
# правила, что и у одиночных форм на /id-folders/{id}: даты принимаются
# любые (задним числом — папки вводятся ретроспективно), сметная
# стоимость отклоняется явным сообщением, если после этого же
# сохранения дата подписания всё ещё не заполнена (свою же submitted
# дату подписания в этом сохранении — тоже считаем действительной, не
# только уже сохранённую раньше).
@app.post("/api/id-folder/{folder_id}/bulk")
def api_id_folder_bulk_update(
    request: Request, folder_id: int,
    check_start_date: str = Form(""), signed_date: str = Form(""), signed_by: str = Form(""),
    ks2_date: str = Form(""), ks2_no: str = Form(""), amount_smeta_rub: str = Form(""),
):
    back_url = "/id-folders/bulk-entry"
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    folder = query_one("select id, name, signed_date, signed_by from id_folder where id=%s", (folder_id,))
    if not folder:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote("Папка не найдена."), status_code=303)

    errors = []
    prefix = f"«{folder['name']}»: "

    check_start_val = None
    if check_start_date.strip():
        check_start_val = _parse_date(check_start_date)
        if not check_start_val:
            errors.append(prefix + "дата начала проверки указана некорректно.")

    signed_val = None
    if signed_date.strip():
        signed_val = _parse_date(signed_date)
        if not signed_val:
            errors.append(prefix + "дата подписания указана некорректно.")

    signed_by_val = signed_by.strip() or None
    if signed_by_val and signed_by_val not in RSK_SIGNERS:
        errors.append(prefix + "недопустимый подписант.")
    effective_signed_by = signed_by_val or folder["signed_by"]
    effective_signed_date_for_by_check = signed_val or folder["signed_date"]
    if signed_val and not effective_signed_by:
        errors.append(prefix + "указана дата подписания без подписанта.")
    if signed_by_val and not effective_signed_date_for_by_check:
        errors.append(prefix + "указан подписант без даты подписания.")

    ks2_val = None
    if ks2_date.strip():
        ks2_val = _parse_date(ks2_date)
        if not ks2_val:
            errors.append(prefix + "дата КС-2 указана некорректно.")
    ks2_no_val = ks2_no.strip() or None
    if bool(ks2_val) != bool(ks2_no_val):
        errors.append(prefix + "дата КС-2 и номер КС-2 заполняются вместе.")

    amount_val = None
    if amount_smeta_rub.strip():
        try:
            amount_val = float(amount_smeta_rub.replace(",", "."))
        except ValueError:
            errors.append(prefix + "сметная стоимость указана некорректно.")
        else:
            if amount_val <= 0:
                errors.append(prefix + "сметная стоимость должна быть больше нуля.")
    # Действует дата подписания ПОСЛЕ этого сохранения — если её заполняют
    # в этой же строке одновременно со сметной стоимостью, это разрешено.
    effective_signed = signed_val or folder["signed_date"]
    if amount_val is not None and not effective_signed:
        errors.append(prefix + "сметная стоимость вводится только после подписания папки.")

    if errors:
        return RedirectResponse(url=back_url + "?err=" + urllib.parse.quote(" ".join(errors)), status_code=303)

    run_in_transaction(lambda cur: cur.execute(
        "update id_folder set check_start_date=%s, signed_date=%s, signed_by=%s, "
        "ks2_date=%s, ks2_no=%s, amount_smeta_rub=%s where id=%s",
        (check_start_val, signed_val, signed_by_val, ks2_val, ks2_no_val, amount_val, folder_id),
    ))
    ok_msg = urllib.parse.quote(f"Папка «{folder['name']}» сохранена.")
    return RedirectResponse(url=f"{back_url}?ok={ok_msg}", status_code=303)


@app.post("/api/manual-volume")
def api_manual_volume_create(request: Request, description: str = Form(...), amount_rub: str = Form(...)):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    if not description.strip():
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Описание обязательно."), status_code=303)
    try:
        amt = float(amount_rub.replace(",", "."))
    except ValueError:
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Сумма указана некорректно."), status_code=303)
    user_id = current_user_id_or_web_form()
    run_in_transaction(lambda cur: cur.execute(
        "insert into id_manual_volume (description, amount_rub, created_by) values (%s, %s, %s)",
        (description.strip(), amt, user_id),
    ))
    return RedirectResponse(url="/id-folders", status_code=303)


@app.post("/api/manual-volume/{volume_id}/delete")
def api_manual_volume_delete(request: Request, volume_id: int):
    if not has_permission(request.state.user, "id-folders:submit"):
        return RedirectResponse(url="/id-folders?err=" + urllib.parse.quote("Нет доступа к сборке папок."), status_code=303)
    run_in_transaction(lambda cur: cur.execute("delete from id_manual_volume where id=%s", (volume_id,)))
    return RedirectResponse(url="/id-folders", status_code=303)


# ====== GET /changes — список ИЗМ ======
@app.get("/changes")
def changes_page(request: Request):
    rows = query(
        f"select id, code, section_code, topic, status, designer_name, request_date, sla_days, "
        f"planned_response_date, actual_response_date, {_change_overdue_expr()} as overdue_days, "
        f"escalation_level, blocked_amount_rub "
        "from change order by blocked_amount_rub desc nulls last, request_date nulls last"
    )
    total = len(rows) if rows else 0
    overdue = sum(1 for r in rows if r['overdue_days']) if rows else 0
    can_edit = has_permission(request.state.user, "changes:submit")
    return render(request, "changes.html", "changes",
                  changes=rows or [], total=total, overdue=overdue, errors=[], values={}, can_edit=can_edit)


@app.post("/api/change/{change_id}/status")
def api_change_update_status(request: Request, change_id: int, status: str = Form(...)):
    # Учётные записи, 29.08.2026: "у change... update в коде отсутствует
    # — статус после создания изменить нельзя. Достроить редактирование."
    if not has_permission(request.state.user, "changes:submit"):
        return JSONResponse({"ok": False, "error": "Нет доступа к форме ИЗМ."}, status_code=403)
    row = query_one("select id, status, request_date, sla_days, planned_response_date from change where id=%s", (change_id,))
    if not row:
        return JSONResponse({"ok": False, "error": "Запись не найдена."}, status_code=404)

    user_id = current_user_id_or_web_form()
    today = object_today()
    actual_response_val = None
    overdue_val = None
    if status in ("SOLUTION_RECEIVED", "IKS_ORDER", "INCLUDED_IN_RD", "ARCHIVED"):
        actual_response_val = today
    elif row["planned_response_date"] and today > row["planned_response_date"]:
        overdue_val = (today - row["planned_response_date"]).days

    def _do(cur):
        cur.execute(
            "select status from change where id=%s for update",
            (change_id,),
        )
        old_status = cur.fetchone()["status"]
        cur.execute(
            "update change set status=%s, actual_response_date=coalesce(%s, actual_response_date), "
            "overdue_days=%s, updated_at=now(), updated_by=%s where id=%s",
            (status, actual_response_val, overdue_val, user_id, change_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, old_value, new_value, reason) "
            "values (%s, 'change', %s, 'status_update', %s, %s, 'форма /changes')",
            (user_id, change_id, json.dumps({"status": old_status}), json.dumps({"status": status})),
        )
    run_in_transaction(_do)
    return RedirectResponse(url="/changes?ok=1", status_code=303)


# ====== POST /changes — добавить ИЗМ ======
@app.post("/changes")
def changes_post(
    request: Request,
    code: str = Form(""),
    section_code: str = Form(""),
    change_number: str = Form(""),
    topic: str = Form(""),
    description: str = Form(""),
    initiator: str = Form("SSR"),
    status: str = Form("IN_WORK_DESIGNER"),
    designer_name: str = Form(""),
    request_date: str = Form(""),
    sla_days: str = Form("14"),
    blocked_amount_rub: str = Form(""),
    request_file_url: str = Form(""),
    comment: str = Form(""),
):
    errors = []
    # Права по веткам, 30.08.2026 — новая находка при перестройке прав:
    # СОЗДАНИЕ ИЗМ вообще не проверяло права (только смена статуса,
    # api_change_update_status, была защищена). Любой залогиненный,
    # включая группу СМР, мог создать запись. Закрываю тем же
    # разрешением, что и смена статуса — симметрично.
    if not has_permission(request.state.user, "changes:submit"):
        errors.append("Нет доступа к форме ИЗМ.")
    if not topic.strip():
        errors.append("Тема обязательна.")

    code_val = code.strip() or None
    section_val = section_code.strip() or None
    desc_val = description.strip() or None
    designer_val = designer_name.strip() or None
    url_val = request_file_url.strip() or None
    comment_val = comment.strip() or None

    # change_number
    num_val = None
    if change_number.strip():
        try:
            num_val = int(change_number)
        except ValueError:
            errors.append("Номер изменения должен быть числом.")

    # request_date
    req_date_val = None
    if request_date.strip():
        req_date_val = _parse_date(request_date)
        if not req_date_val:
            errors.append("Дата запроса указана некорректно (ДД.ММ.ГГГГ).")

    # sla_days
    sla_val = 14
    if sla_days.strip():
        try:
            sla_val = int(sla_days)
        except ValueError:
            errors.append("SLA должен быть числом.")

    # blocked_amount_rub
    amt_val = None
    if blocked_amount_rub.strip():
        try:
            amt_val = float(blocked_amount_rub.replace(',', '.'))
        except ValueError:
            errors.append("Сумма указана некорректно.")

    # planned_response_date = request_date + sla_days
    plan_resp_val = None
    overdue_val = None
    if req_date_val:
        
        plan_resp_val = req_date_val + _td(days=sla_val)
        today = _dt.now().date()
        if not status or status in ('DRAFT', 'REQUEST_SENT', 'IN_WORK_DESIGNER'):
            if today > plan_resp_val:
                overdue_val = (today - plan_resp_val).days

    def _render_error():
        rows = query(
            f"select code, section_code, topic, status, designer_name, request_date, sla_days, "
            f"planned_response_date, actual_response_date, {_change_overdue_expr()} as overdue_days, "
            f"escalation_level, blocked_amount_rub "
            "from change order by blocked_amount_rub desc nulls last"
        )
        total = len(rows) if rows else 0
        overdue = sum(1 for r in rows if r['overdue_days']) if rows else 0
        return render(request, "changes.html", "changes",
                      changes=rows or [], total=total, overdue=overdue,
                      errors=errors, values={
                          "code": code, "section_code": section_code, "change_number": change_number,
                          "topic": topic, "description": description, "initiator": initiator,
                          "status": status, "designer_name": designer_name, "request_date": request_date,
                          "sla_days": sla_days, "blocked_amount_rub": blocked_amount_rub,
                          "request_file_url": request_file_url, "comment": comment,
                      })

    if errors:
        return _render_error()

    # Auto-generate code if empty
    if not code_val and section_val and num_val:
        code_val = f"ИЗМ-{num_val}-{section_val}"
    elif not code_val:
        next_id = query_one("select coalesce(max(id),0)+1 as next from change")
        code_val = f"ИЗМ-AUTO-{next_id['next']}"

    def _insert_change(cur):
        cur.execute(
            """insert into change 
            (code, section_code, change_number, topic, description, initiator, status, designer_name,
             request_date, sla_days, planned_response_date, overdue_days, blocked_amount_rub,
             request_file_url, comment)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id""",
            (code_val, section_val, num_val, topic.strip(), desc_val, initiator, status, designer_val,
             req_date_val, sla_val, plan_resp_val, overdue_val, amt_val, url_val, comment_val)
        )
        return cur.fetchone()['id']
    try:
        run_in_transaction(_insert_change)
    except psycopg2.errors.UniqueViolation:
        # Код ИЗМ генерируется из раздела+номера (см. выше) без проверки на
        # дубликат перед вставкой — раньше падало сырым 500 вместо понятной
        # ошибки, если такой шифр уже есть (координатор поймал 04.09.2026).
        errors.append(f"Изменение с шифром «{code_val}» уже существует — проверьте раздел и номер изменения.")
        return _render_error()
    return RedirectResponse(url="/changes?ok=1", status_code=303)


# ====== GET/POST /changes/{id} — карточка ИЗМ (правка, координатор 04.09.2026) ======
# Шифр (code) неизменяем после создания — это опознавательный номер записи,
# а не редактируемое поле; раздел/номер изменения тоже read-only по той же
# причине (их правка означала бы фактически другую запись). Редактируются
# только содержательные поля: тема, описание, статус, проектировщик, даты,
# SLA, сумма блокировки, комментарий — тот же набор допущений, что и в
# /changes POST при создании.
@app.get("/changes/{change_id}")
def change_detail_page(request: Request, change_id: int):
    row = query_one("select * from change where id=%s", (change_id,))
    if not row:
        return RedirectResponse(url="/changes", status_code=303)
    can_edit = has_permission(request.state.user, "changes:submit")
    return render(request, "change_detail.html", "changes",
                  c=row, can_edit=can_edit, errors=[])


@app.post("/changes/{change_id}")
def change_detail_post(
    request: Request,
    change_id: int,
    topic: str = Form(""),
    description: str = Form(""),
    status: str = Form("IN_WORK_DESIGNER"),
    designer_name: str = Form(""),
    request_date: str = Form(""),
    sla_days: str = Form("14"),
    blocked_amount_rub: str = Form(""),
    comment: str = Form(""),
):
    row = query_one("select * from change where id=%s", (change_id,))
    if not row:
        return RedirectResponse(url="/changes", status_code=303)
    if not has_permission(request.state.user, "changes:submit"):
        return RedirectResponse(url=f"/changes/{change_id}", status_code=303)

    errors = []
    if not topic.strip():
        errors.append("Тема обязательна.")

    desc_val = description.strip() or None
    designer_val = designer_name.strip() or None
    comment_val = comment.strip() or None

    req_date_val = None
    if request_date.strip():
        req_date_val = _parse_date(request_date)
        if not req_date_val:
            errors.append("Дата запроса указана некорректно (ДД.ММ.ГГГГ).")

    sla_val = 14
    if sla_days.strip():
        try:
            sla_val = int(sla_days)
        except ValueError:
            errors.append("SLA должен быть числом.")

    amt_val = None
    if blocked_amount_rub.strip():
        try:
            amt_val = float(blocked_amount_rub.replace(',', '.'))
        except ValueError:
            errors.append("Сумма указана некорректно.")

    plan_resp_val = None
    overdue_val = None
    if req_date_val:
        plan_resp_val = req_date_val + _td(days=sla_val)
        today = _dt.now().date()
        if not status or status in ('DRAFT', 'REQUEST_SENT', 'IN_WORK_DESIGNER'):
            if today > plan_resp_val:
                overdue_val = (today - plan_resp_val).days

    actual_response_val = row["actual_response_date"]
    if status == "SOLUTION_RECEIVED" and row["status"] != "SOLUTION_RECEIVED":
        actual_response_val = object_today()
    elif status != "SOLUTION_RECEIVED":
        actual_response_val = None

    if errors:
        merged = dict(row)
        merged.update({
            "topic": topic, "description": description, "status": status,
            "designer_name": designer_name, "request_date": request_date,
            "sla_days": sla_days, "blocked_amount_rub": blocked_amount_rub, "comment": comment,
        })
        return render(request, "change_detail.html", "changes",
                      c=merged, can_edit=True, errors=errors)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            "update change set topic=%s, description=%s, status=%s, designer_name=%s, "
            "request_date=%s, sla_days=%s, planned_response_date=%s, overdue_days=%s, "
            "blocked_amount_rub=%s, comment=%s, actual_response_date=%s, "
            "updated_at=now(), updated_by=%s where id=%s",
            (topic.strip(), desc_val, status, designer_val, req_date_val, sla_val,
             plan_resp_val, overdue_val, amt_val, comment_val, actual_response_val,
             user_id, change_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, old_value, new_value, reason) "
            "values (%s, 'change', %s, 'edit', %s, %s, 'форма /changes/{id}')",
            (user_id, change_id,
             json.dumps({"topic": row["topic"], "status": row["status"]}, default=str),
             json.dumps({"topic": topic.strip(), "status": status}, default=str)),
        )
    run_in_transaction(_do)
    return RedirectResponse(url=f"/changes/{change_id}?ok=1", status_code=303)


# GET/POST /prescriptions, /api/prescription/{id}/status — убраны
# 06.09.2026 вместе с /export/prescriptions.csv (см. пометку выше).


# ====== Обновлённый GET /dashboard (home) с передачей статистики новых разделов ======
@app.get("/dashboard")
def home_v2(request: Request):
    works_total = query_one("select count(*) as n from work")["n"]
    # Статус — вычисляется из fact_pct (_work_status_expr), не читает
    # столбец work.status: тот не обновлялся неделями, хотя факт вводится
    # регулярно (координатор, 08.09.2026).
    by_status = query(
        f"select {_work_status_expr(None)} as status, count(*) as n from work group by 1 order by n desc"
    )
    needs_review = query_one(
        "select count(*) as n from work where data_quality_flag='needs_review'"
    )["n"]
    unresolved = query_one("select count(*) as n from import_unresolved_cell")["n"]

    avg_pct = query_one(
        "select round(avg(fact_pct)::numeric, 1) as v, count(fact_pct) as n "
        "from work where fact_pct is not null"
    )

    last_actual_date = query_one(
        "select max(date) as d from daily_progress where actual_crew is not null"
    )["d"]

    today_totals = None
    people_deficit = None
    people_surplus = None
    if last_actual_date:
        today_totals = query_one(
            LATEST_DP_CTE + """
            select sum(planned_crew) as planned, sum(actual_crew) as actual
            from latest_dp where date=%s
            """,
            (last_actual_date,),
        )
        # Знак "Дефицита" (координатор, 31.08.2026): раньше карточка
        # печатала голую разность план-факт, включая отрицательные
        # значения под подписью "Дефицит" — при факте больше плана это
        # читалось как "не хватает -3 человек", хотя на деле был избыток.
        # Теперь дефицит никогда не отрицательный (max(0, ...)), избыток
        # показывается отдельной плиткой и только когда он реально есть,
        # при точном равенстве обе величины отсутствуют — шаблон рисует
        # "-" вместо нуля.
        if today_totals and today_totals.get("planned") is not None:
            diff = (today_totals["planned"] or 0) - (today_totals["actual"] or 0)
            if diff > 0:
                people_deficit = diff
            elif diff < 0:
                people_surplus = -diff

    top_comments = query(
        LATEST_DP_CTE + """
        select comment, count(*) as n
        from latest_dp
        where comment is not null and comment <> ''
        group by comment
        order by n desc
        limit 5
        """
    )

    blockers_total = query_one("select count(*) as n from blocker")["n"]
    subcontractors_total = query_one("select count(*) as n from subcontractor")["n"]

    # СМР-задание 29.08.2026 (п.4б, Якименко А.И.): "blockers_total" уже
    # считался, но ни разу не выводился в home.html — стоп-факторы были
    # невидимы на дашборде. Отдельно считаем "активно" (blocker.status,
    # не "resolved") — это то, что реально мешает сейчас, не вся история.
    blockers_active_total = query_one(
        "select count(*) as n from blocker where status='active'"
    )["n"]
    blockers_active = query(
        "select b.id, b.blocker_type, b.description, b.created_at::date as since, "
        "b.expected_resolution_date, w.code as work_code, w.name as work_name "
        "from blocker b left join work w on w.id=b.work_id "
        "where b.status='active' order by b.created_at asc limit 5"
    )

    # Новые статистики для карточек навигации. Источник — id_form_row
    # (актуальные 15 категорий ПТО, без ОПВ/Н), не устаревший id_package
    # (координатор, 04.09.2026 — тот же перевод, что на странице /id-packages).
    # "Подписано" — по последней записи id_form_entry этого раздела, статус
    # с кодом id_form_status.code='Подписано'; "заблокировано" — активная
    # (unblocked_at is null) блокировка ИЗМ через id_form_block.
    id_stats_row = query_one(LATEST_ID_FORM_ENTRY_CTE + """
        select
            count(*) as total,
            count(*) filter (
                where s.code = 'Подписано'
            ) as signed,
            count(*) filter (
                where exists (
                    select 1 from id_form_block b
                    where b.row_id = r.id and b.unblocked_at is null
                )
            ) as blocked
        from id_form_row r
        join id_form_tab t on t.id = r.tab_id
        left join latest_id_entry le on le.row_id = r.id
        left join id_form_status s on s.id = le.status_id
        where t.code not in ('opv', 'n')
    """) or {"total": 0, "signed": 0, "blocked": 0}

    change_stats_row = query_one(f"""
        select
            count(*) as total,
            count(*) filter (where {_change_overdue_expr()} is not null) as overdue
        from change
        where status not in ('INCLUDED_IN_RD', 'ARCHIVED')
    """) or {"total": 0, "overdue": 0}

    # Свой блок "Обзор РСК" (координатор, 06.09.2026) — РСК теперь
    # равноправный раздел меню рядом с СМР/ИД, не подраздел ИД. Те же
    # цифры, что на /rsk/dashboard (компактная функция, не дублируем SQL).
    rsk_dash = compute_rsk_dashboard_stats()

    crit = get_criticality_data()
    evm = get_evm_data()
    # "Отставание от графика" — раньше считалось инлайн в Jinja
    # (home.html), показатель без функции-владельца (координатор,
    # 08.09.2026, аудит целостности). Формула не изменилась, только
    # переехала в Python рядом с остальными расчётами этой страницы.
    schedule_lag_ratio = (
        crit["elapsed_pct"] / evm["weighted_pct"]
        if crit.get("elapsed_pct") is not None and evm.get("weighted_pct")
        else None
    )

    return render(
        request, "home.html", "dashboard",
        works_total=works_total, by_status=by_status,
        schedule_lag_ratio=schedule_lag_ratio,
        needs_review=needs_review, unresolved=unresolved,
        avg_pct=avg_pct, last_actual_date=last_actual_date,
        today_totals=today_totals, top_comments=top_comments,
        people_deficit=people_deficit, people_surplus=people_surplus,
        blockers_total=blockers_total, subcontractors_total=subcontractors_total,
        blockers_active_total=blockers_active_total, blockers_active=blockers_active,
        crit=crit, evm=evm,
        id_stats=id_stats_row,
        change_stats=change_stats_row,
        rsk_dash=rsk_dash,
        folder_stats=compute_id_folder_stats(),
        folder_stages=ID_FOLDER_STAGES, folder_stage_labels=ID_FOLDER_STAGE_LABELS,
    )



# =======================================================================
# Контур ИД — форма ввода по ответам ПТО (28.08.2026,
# TM35_ID_TZ_po_otvetam_PTO.md). Единица учёта — РАЗДЕЛ (строка
# вкладки), термин «папка» в интерфейсе не используется — папка
# появляется позже из нескольких подписанных разделов (сборка папок —
# отдельная форма «Выполнение», ещё не реализована). id_package
# (устаревший разовый импорт от 17.08.2026) больше нигде не читается —
# /id-packages, /export/id-packages.csv и плитка дашборда переведены на
# id_form_row 04.09.2026, таблицу саму не удалял (см. правило проекта).
#
# Атом записи по факту данных Excel — пара (строка-раздел/конструкция,
# вид работ): статус-матрица в исходнике именно такая. Формально ПТО
# сказал «одна строка — один раздел», но объявленные поля формы (2 —
# раздел/конструкция, 3 — вид работ, ОТДЕЛЬНО) и сама структура таблиц
# Excel этому не противоречат — реализовано каскадом (вкладка → раздел/
# конструкция → вид работ), work_type_id в id_form_entry допускает NULL
# на случай вкладок без деления на виды работ. Открытый вопрос — в
# описи находок, не решён молча.
# =======================================================================

RU_STOPPER_NOTE = "причина остановки, не стадия конвейера"


RSK_SIGNERS = ["Карась Э.В.", "Зотов М.Н.", "Карпенко Р.В."]

# Стоп-фактор — раньше произвольный текст, координатор попросил закрытый
# список (докс "Вопросы к базе по ТМ-35", п.3, 06.09.2026): только
# обстоятельства, не зависящие от ответственного лица целиком. Замечание
# Болтика В.Н. 07.09.2026 — справочник должен пополняться без правки кода,
# поэтому значения переехали из константы в таблицу id_stop_factor
# (миграция 026); функция читает её каждый раз, не кэширует — таблица
# из нескольких строк, лишний запрос дешевле риска показать устаревший
# список после правки координатором через БД напрямую.
def get_stop_factors():
    rows = query("select description from id_stop_factor where active order by display_order, id")
    return [r["description"] for r in rows]

# Уточнённое ТЗ координатора, 03.09.2026 (докс + прямой список): вкладки
# без ответственного в исходном xlsx ПТО — их разделы подмешиваются в
# список «Раздел» независимо от того, кто выбран Ответственным, а не
# исчезают из формы. met_konstr — координатор явно сказал "без
# ответственного, временно доступна всем"; opv/n того же класса — есть в
# БД (23 и 46 разделов), но их вообще нет в списке из 15 категорий
# координатора (найдено при планировании этапа 1, подтверждено
# координатором — тот же принцип, что и met_konstr).

# Координатор, 03.09.2026: в папке ИД на Яндекс.Диске (источник истины
# для разделов) ровно 15 xlsx-категорий — opv/n там нет, это не входит
# в 15 настоящих категорий (была ошибочная догадка сессии раньше в тот
# же день, что раз есть в БД — значит легитимные "бесхозные", как
# Металлоконструкции; это не так, убраны из формы совсем).
# Металлоконструкции тоже была тут ошибочно — в файле-источнике у неё
# есть ответственный (Завгородний А.В., все 62 строки), просто я его
# сначала неверно удалил из id_form_responsible, потом восстановил.
# Список "бесхозных" сейчас пуст — все 15 категорий имеют владельца.
UNASSIGNED_TAB_CODES = []


@app.get("/id-entry")
def id_entry_page(request: Request):
    # Форма 1, этап 1 уточнённого ТЗ (03.09.2026): первый шаг — не вкладка,
    # а Ответственный (список из 7 ФИО = роль "Ответственный за ввод
    # данных" в id_form_responsible, после сверки с xlsx ПТО в этой же
    # сессии). Порядок — по display_order, заданному координатором.
    responsible_rows = query(
        "select distinct on (full_name) full_name, display_order "
        "from id_form_responsible where role='Ответственный за ввод данных' "
        "order by full_name, display_order"
    )
    responsible_names = [r["full_name"] for r in sorted(responsible_rows, key=lambda r: r["display_order"])]
    # Список видов работ для фильтра реестра (замечание Болтика В.Н.,
    # 07.09.2026, п.5) — общий по всем вкладкам, названия иногда
    # совпадают между вкладками (например, "Земляные работы"), это
    # ожидаемо: фильтр по имени, не по конкретной вкладке.
    work_type_names = [r["name"] for r in query("select distinct name from id_form_work_type order by name")]
    return render(request, "id_entry.html", "id-entry",
                  responsible_names=responsible_names, rsk_signers=RSK_SIGNERS,
                  stop_factors=get_stop_factors(), work_type_names=work_type_names)


@app.get("/api/id-form/by-responsible")
def api_id_form_by_responsible(request: Request, name: str):
    # Заменяет вкладку как точку входа (было: выбрать вкладку → ответственный
    # подгружался из неё). Теперь наоборот: человек уже известен, вкладки
    # определяются им — плюс всегда "бесхозные" (UNASSIGNED_TAB_CODES).
    # Возвращаем сразу бандл по каждой вкладке (rows/work_types/statuses),
    # чтобы при выборе конкретного раздела не делать второй запрос.
    tab_rows = query(
        "select distinct t.id, t.code, t.label from id_form_tab t "
        "join id_form_responsible r on r.tab_id=t.id "
        "where r.role='Ответственный за ввод данных' and r.full_name=%s",
        (name,),
    )
    tab_ids = {t["id"]: t for t in tab_rows}
    unassigned = query(
        "select id, code, label from id_form_tab where code = any(%s)",
        (UNASSIGNED_TAB_CODES,),
    )
    for t in unassigned:
        tab_ids[t["id"]] = t

    # Тот же принцип, что раньше ограничивал видимость вкладок на самой
    # странице (перенесено сюда, т.к. страница больше не перечисляет
    # вкладки сама — см. id_entry_page): чужие вкладки человек видеть не
    # должен, кроме координатора/анонимного просмотра/группы без точечных
    # разрешений.
    user = request.state.user
    if user and not is_admin(user):
        perms = user_permissions(user)
        id_tab_perms = {p for p in perms if p.startswith("id_tab:")}
        if "zone:id" not in perms and id_tab_perms:
            tab_ids = {tid: t for tid, t in tab_ids.items() if f"id_tab:{t['code']}" in perms}

    tabs_out = []
    for tab_id, t in sorted(tab_ids.items(), key=lambda kv: kv[1]["label"]):
        rows = query(
            "select id, section_label, construction_label, foundation_label from id_form_row "
            "where tab_id=%s order by source_row", (tab_id,),
        )
        work_types = query(
            "select id, name from id_form_work_type where tab_id=%s order by display_order", (tab_id,),
        )
        statuses = query(
            "select id, code, label, is_stopper from id_form_status where tab_id=%s order by display_order",
            (tab_id,),
        )
        tabs_out.append({
            "tab_id": tab_id, "tab_code": t["code"], "tab_label": t["label"],
            "rows": rows, "work_types": work_types, "statuses": statuses,
        })
    return {"tabs": tabs_out}


@app.get("/api/id-form/tab-data")
def api_id_form_tab_data(tab_id: int):
    work_types = query(
        "select id, name, responsible_name, signer_name from id_form_work_type "
        "where tab_id=%s order by display_order", (tab_id,),
    )
    rows = query(
        "select id, section_label, construction_label, foundation_label from id_form_row "
        "where tab_id=%s order by source_row", (tab_id,),
    )
    statuses = query(
        "select id, code, label, is_stopper from id_form_status "
        "where tab_id=%s order by display_order", (tab_id,),
    )
    # "Ответственный" — из настоящего каталога роль→ФИО (версия справочников
    # от 27.08.2026), не из колонок видов работ (та схема не подходила для
    # этого среза данных — см. миграцию 015). Дубли ФИО по разным ролям
    # схлопываются, показывается уникальный список имён.
    responsible_rows = query(
        "select role, full_name from id_form_responsible where tab_id=%s order by display_order",
        (tab_id,),
    )
    names = []
    seen = set()
    for r in responsible_rows:
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            names.append(r["full_name"])
    return {
        "work_types": work_types, "rows": rows, "statuses": statuses,
        "responsible": names, "responsible_roles": responsible_rows,
    }


@app.get("/api/id-form/registry")
def api_id_form_registry(
    tab_id: int = 0, responsible_name: str = "", work_type_name: str = "",
    section_query: str = "", offset: int = 0, limit: int = 50,
):
    # Замечание Болтика В.Н., 07.09.2026, п.5: с жёстким лимитом 200
    # человек после выходного не мог понять, весь ли объём перед глазами —
    # к моменту, когда он начинал ввод, уже накапливалось 200 чужих
    # записей. Заменено на фильтры (ответственный/вид работ/раздел) +
    # постраничную выдачу с общим числом найденного — сервер отдаёт
    # ровно один экран (limit, по умолчанию 50), а не всё разом.
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    conditions = []
    params = []
    if tab_id:
        conditions.append("e.tab_id=%s")
        params.append(tab_id)
    if responsible_name:
        conditions.append("e.responsible_name=%s")
        params.append(responsible_name)
    if work_type_name:
        conditions.append("wt.name=%s")
        params.append(work_type_name)
    if section_query:
        conditions.append("(r.section_label ilike %s or r.construction_label ilike %s)")
        like = f"%{section_query}%"
        params.extend([like, like])
    where = ("where " + " and ".join(conditions)) if conditions else ""

    base_sql = f"""
        with latest as (
            select distinct on (row_id, work_type_id) *
            from id_form_entry
            order by row_id, work_type_id, created_at desc
        )
        select e.id, e.tab_id, t.label as tab_label, e.row_id, e.work_type_id,
               r.section_label, r.construction_label,
               wt.name as work_type_name, e.responsible_name, e.status_id, s.code as status_code,
               coalesce(s.label, 'статус не задан') as status_label,
               (s.id is null) as status_missing,
               s.is_stopper, e.status_date, e.planned_rsk_date,
               e.rsk_signer_name, e.comment, e.created_at,
               bl.description as stop_factor,
               b.id as block_id, b.change_ref, b.blocked_at
        from latest e
        join id_form_tab t on t.id = e.tab_id
        join id_form_row r on r.id = e.row_id
        left join id_form_work_type wt on wt.id = e.work_type_id
        left join id_form_status s on s.id = e.status_id
        left join blocker bl on bl.id = e.blocker_id
        left join id_form_block b on b.row_id = e.row_id
            and (b.work_type_id = e.work_type_id or (b.work_type_id is null and e.work_type_id is null))
            and b.unblocked_at is null
        {where}
    """
    total = query_one(f"select count(*) as n from ({base_sql}) sub", tuple(params))["n"]
    rows = query(
        base_sql + " order by e.created_at desc limit %s offset %s",
        tuple(params) + (limit, offset),
    )
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


# Точечный поиск последней записи по конкретному (раздел, вид работ) —
# для автоподстановки в форму 1, когда выбирается уже заполнявшийся атом
# (координатор, докс "Вопросы к базе по ТМ-35", п.1-2, 06.09.2026: раньше
# форма всегда открывалась пустой, даже если по этому же разделу+виду
# работ уже что-то вводили). Тот же набор полей, что и в реестре выше, но
# без лимита в 200 записей и с фильтром по конкретной паре, а не общий срез.
@app.get("/api/id-form/entry")
def api_id_form_entry_lookup(row_id: int, work_type_id: str = ""):
    wt_val = int(work_type_id) if work_type_id.strip() else None
    row = query_one(
        """
        select e.id, e.status_id, s.code as status_code, e.status_date, e.planned_rsk_date,
               e.rsk_signer_name, e.comment, bl.description as stop_factor,
               b.id as block_id, b.change_ref
        from id_form_entry e
        left join id_form_status s on s.id = e.status_id
        left join blocker bl on bl.id = e.blocker_id
        left join id_form_block b on b.row_id = e.row_id
            and (b.work_type_id = e.work_type_id or (b.work_type_id is null and e.work_type_id is null))
            and b.unblocked_at is null
        where e.row_id = %s and (e.work_type_id = %s or (e.work_type_id is null and %s::bigint is null))
        order by e.created_at desc
        limit 1
        """,
        (row_id, wt_val, wt_val),
    )
    return {"entry": row}


@app.post("/api/id-entry")
def api_id_entry_create(
    request: Request,
    tab_id: int = Form(...), row_id: int = Form(...), work_type_id: str = Form(""),
    responsible_name: str = Form(...), status_id: str = Form(""),
    status_date: str = Form(""), planned_rsk_date: str = Form(""),
    stop_factor: str = Form(""), rsk_signer_name: str = Form(""), comment: str = Form(""),
):
    errors = []

    # Учётные записи, 29.08.2026: заполняет форму только ответственный за
    # вкладку. AuthMiddleware уже гарантировал вход — здесь проверяем,
    # что у ЭТОГО человека есть разрешение именно на эту вкладку, а не
    # только что он вообще куда-то вошёл. Проверка на сервере, не только
    # скрытие вкладки в интерфейсе — заблокированный запрос не должен
    # тихо сохраняться в обход спрятанного select'а.
    tab_row = query_one("select code from id_form_tab where id=%s", (tab_id,))
    if not tab_row or not has_permission(request.state.user, f"id_tab:{tab_row['code']}"):
        return JSONResponse(
            {"ok": False, "errors": ["Нет доступа к этой вкладке — обратитесь к координатору."]},
            status_code=403,
        )

    if not query_one("select id from id_form_row where id=%s and tab_id=%s", (row_id, tab_id)):
        errors.append("Раздел/конструкция не найдены на выбранной вкладке.")

    wt_id_val = None
    if work_type_id.strip():
        try:
            wt_id_val = int(work_type_id)
        except ValueError:
            errors.append("Вид работ указан некорректно.")
        else:
            if not query_one("select id from id_form_work_type where id=%s and tab_id=%s", (wt_id_val, tab_id)):
                errors.append("Вид работ не найден на выбранной вкладке.")

    # Подготовка пилота, 30.08.2026 (решение координатора): статус
    # обязателен ТОЛЬКО там, где у вкладки вообще есть справочник
    # статусов — семь вкладок (n, sodk, opv, izolyaciya, elektrika,
    # met_konstr, lotki) справочника не имеют вообще, требовать выбор
    # там, где нечего выбрать, значит блокировать людей физически.
    # Заглушку не подставляем — пусто значит пусто, честно.
    status_id_val = None
    if status_id.strip():
        try:
            status_id_val = int(status_id)
        except ValueError:
            errors.append("Статус указан некорректно.")
        else:
            if not query_one("select id from id_form_status where id=%s and tab_id=%s", (status_id_val, tab_id)):
                errors.append("Статус не найден на выбранной вкладке.")
    else:
        tab_has_statuses = query_one("select id from id_form_status where tab_id=%s limit 1", (tab_id,))
        if tab_has_statuses:
            errors.append("«Статус» обязателен.")

    resp_val = responsible_name.strip()
    if not resp_val:
        errors.append("«Ответственный» обязателен.")

    status_date_val = object_today()
    if status_date.strip():
        try:
            status_date_val = date_cls.fromisoformat(status_date.strip())
        except ValueError:
            errors.append("Дата статуса указана некорректно.")

    planned_val = None
    if planned_rsk_date.strip():
        try:
            planned_val = date_cls.fromisoformat(planned_rsk_date.strip())
        except ValueError:
            errors.append("Планируемая дата передачи в РСК указана некорректно.")

    # Подписант РСК — координатор, 03.09.2026 (поправка после первой
    # реализации): РСК — внешние сотрудники, ничего в системе не
    # подписывают, поле просто отмечает, кто из них подписал документ.
    # Обычное поле формы, не отдельное действие и не смена статуса —
    # статус меняется только через «Статус» ниже, как обычно.
    rsk_signer_val = rsk_signer_name.strip() or None
    if rsk_signer_val and rsk_signer_val not in RSK_SIGNERS:
        errors.append("Недопустимый подписант РСК.")

    stop_val = stop_factor.strip() or None
    if stop_val and stop_val not in get_stop_factors():
        errors.append("Недопустимый стоп-фактор — выберите из списка.")

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()
    comment_val = comment.strip() or None

    def _do(cur):
        blocker_id_val = None
        if stop_val:
            # Существующий механизм blocker переиспользован, не новый.
            # work_id тут не про СМР — привязки к id_form_row у blocker
            # нет (не расширяем его схему в рамках этой задачи), стоп-
            # фактор ИД просто фиксируется отдельной строкой blocker с
            # описанием; связь видна через сам текст комментария записи.
            # blocker_type='id_docs' (миграция 014) — раньше был
            # 'design_decision', семантически не то (тот — про СМР),
            # не смешиваем разнородные причины в одном типе (см. находку
            # про "дождь + отсутствие ГСМ" одним типом "погода").
            cur.execute(
                "insert into blocker (blocker_type, description, status, created_at) "
                "values ('id_docs', %s, 'active', now()) returning id",
                (stop_val,),
            )
            blocker_id_val = cur.fetchone()["id"]

            # Автоблокировка при выборе стоп-фактора (координатор, докс
            # "Вопросы к базе по ТМ-35", п.4, 06.09.2026: "если стоп-фактор
            # будет активным... и будет производиться блокировка, то не
            # вижу необходимости в отдельном окне «Блокировка ИЗМ»") —
            # отдельная форма/окно на странице убраны, id_form_block (тот
            # же механизм, что раньше ставился только через неё — бейдж
            # «заблокировано» и ссылка «снять» в реестре, и счётчик
            # «заблокировано» на дашборде) выставляется отсюда напрямую.
            # change_ref — не номер ИЗМ (тут его чаще всего нет), а сам
            # текст причины: понятнее в бейдже реестра, чем пустое
            # "заблокировано" без пояснения.
            # Не дублируем: если по этому же (row_id, work_type_id) уже
            # есть активная блокировка — не плодим вторую, только
            # обновляем причину/комментарий у существующей.
            cur.execute(
                "select id from id_form_block where row_id=%s "
                "and (work_type_id=%s or (work_type_id is null and %s::bigint is null)) "
                "and unblocked_at is null",
                (row_id, wt_id_val, wt_id_val),
            )
            existing_block = cur.fetchone()
            if existing_block:
                cur.execute(
                    "update id_form_block set change_ref=%s, comment=%s where id=%s",
                    (stop_val, comment_val, existing_block["id"]),
                )
            else:
                cur.execute(
                    "insert into id_form_block (row_id, work_type_id, change_ref, blocked_at, comment, created_by) "
                    "values (%s,%s,%s, current_date, %s, %s) returning id",
                    (row_id, wt_id_val, stop_val, comment_val, user_id),
                )
                new_block_id = cur.fetchone()["id"]
                cur.execute(
                    "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
                    "values (%s, 'id_form_block', %s, 'id_block_set', %s, 'форма /id-entry — автоблокировка по стоп-фактору')",
                    (user_id, new_block_id, json.dumps(
                        {"row_id": row_id, "work_type_id": wt_id_val, "change_ref": stop_val}, ensure_ascii=False)),
                )

        # прежнее значение статуса для этого же атома (row_id, work_type_id) — для audit_log.old_value.
        # LEFT JOIN, не JOIN — 30.08.2026: прежняя запись сама могла быть
        # без статуса (пустая вкладка), INNER JOIN бы её тихо потерял.
        cur.execute(
            "select s.code as status_code, s.label as status_label, e.status_date, e.responsible_name "
            "from id_form_entry e left join id_form_status s on s.id=e.status_id "
            "where e.row_id=%s and (e.work_type_id=%s or (e.work_type_id is null and %s::bigint is null)) "
            "order by e.created_at desc limit 1",
            (row_id, wt_id_val, wt_id_val),
        )
        prev = cur.fetchone()
        old_value = (
            {"status_code": prev["status_code"], "status_label": prev["status_label"],
             "status_date": str(prev["status_date"]), "responsible_name": prev["responsible_name"]}
            if prev else None
        )

        cur.execute(
            """insert into id_form_entry
               (tab_id, row_id, work_type_id, responsible_name, status_id, status_date,
                planned_rsk_date, blocker_id, rsk_signer_name, comment, created_by)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (tab_id, row_id, wt_id_val, resp_val, status_id_val, status_date_val,
             planned_val, blocker_id_val, rsk_signer_val, comment_val, user_id),
        )
        entry_id = cur.fetchone()["id"]

        # 30.08.2026: status_id_val может быть None (вкладка без справочника) —
        # тогда статус в журнале честно "не задан", не подставляем чужой код.
        if status_id_val is not None:
            cur.execute("select code, label from id_form_status where id=%s", (status_id_val,))
            new_status = cur.fetchone()
            new_value = {"status_code": new_status["code"], "status_label": new_status["label"],
                         "status_date": str(status_date_val), "responsible_name": resp_val}
        else:
            new_value = {"status_code": None, "status_label": "статус не задан",
                         "status_date": str(status_date_val), "responsible_name": resp_val}

        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, old_value, new_value, reason) "
            "values (%s, 'id_form_entry', %s, 'id_entry_status_change', %s, %s, 'форма /id-entry')",
            (user_id, entry_id, json.dumps(old_value, ensure_ascii=False) if old_value else None,
             json.dumps(new_value, ensure_ascii=False)),
        )
        return entry_id

    entry_id = run_in_transaction(_do)
    return {"ok": True, "id": entry_id}


@app.post("/api/id-block")
def api_id_block_create(
    request: Request,
    row_id: int = Form(...), work_type_id: str = Form(""),
    change_ref: str = Form(""), comment: str = Form(""),
):
    # Часть 1 переппроверки доступа, 30.08.2026: этот путь и unblock ниже
    # не имели проверки прав на вкладку вообще (только "залогинен ли
    # кто-то" через AuthMiddleware) — тот же класс пробела, который для
    # /api/id-entry уже закрыт правильно 29.08. Живой тест под denisov
    # подтвердил: блокировка чужой строки реально создавалась (200,
    # новая строка id_form_block). Проверка — тем же способом, что у
    # api_id_entry_create (has_permission по коду вкладки строки).
    tab_row = query_one(
        "select t.code from id_form_row r join id_form_tab t on t.id=r.tab_id where r.id=%s",
        (row_id,),
    )
    if not tab_row or not has_permission(request.state.user, f"id_tab:{tab_row['code']}"):
        return JSONResponse({"ok": False, "errors": ["Нет доступа к этой вкладке ИД."]}, status_code=403)

    wt_id_val = int(work_type_id) if work_type_id.strip() else None
    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            "insert into id_form_block (row_id, work_type_id, change_ref, blocked_at, comment, created_by) "
            "values (%s,%s,%s, current_date, %s, %s) returning id",
            (row_id, wt_id_val, change_ref.strip() or None, comment.strip() or None, user_id),
        )
        block_id = cur.fetchone()["id"]
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'id_form_block', %s, 'id_block_set', %s, 'форма /id-entry — блокировка ИЗМ')",
            (user_id, block_id, json.dumps({"row_id": row_id, "work_type_id": wt_id_val,
                                             "change_ref": change_ref.strip() or None}, ensure_ascii=False)),
        )
        return block_id

    block_id = run_in_transaction(_do)
    return {"ok": True, "id": block_id}


@app.post("/api/id-block/{block_id}/unblock")
def api_id_block_unset(request: Request, block_id: int):
    # Та же правка, что у api_id_block_create выше (см. комментарий там).
    block_row = query_one(
        "select t.code from id_form_block b "
        "join id_form_row r on r.id=b.row_id join id_form_tab t on t.id=r.tab_id "
        "where b.id=%s",
        (block_id,),
    )
    if not block_row or not has_permission(request.state.user, f"id_tab:{block_row['code']}"):
        return JSONResponse({"ok": False, "errors": ["Нет доступа к этой вкладке ИД."]}, status_code=403)

    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            "update id_form_block set unblocked_at=current_date where id=%s and unblocked_at is null "
            "returning row_id, work_type_id",
            (block_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
                "values (%s, 'id_form_block', %s, 'id_block_unset', %s, 'форма /id-entry — снятие блокировки ИЗМ')",
                (user_id, block_id, json.dumps({"row_id": row["row_id"], "work_type_id": row["work_type_id"]}, ensure_ascii=False)),
            )
        return row

    row = run_in_transaction(_do)
    if not row:
        return JSONResponse({"ok": False, "errors": ["Блокировка не найдена или уже снята."]}, status_code=404)
    return {"ok": True}


# =======================================================================
# Контур РСК — ветка меню (координатор, 06.09.2026, "Ветка РСК: архитектура
# раздела"). Архитектура и каркас — точность формулировок и отделка
# отдельным этапом, здесь сознательно не тратится время на то, чтобы
# дожать каждую мелочь.
#
# Два слоя данных, не смешивать:
#   слой 1 (акт, неизменяемый) — rsk_act/rsk_act_item/rsk_violation;
#     пишет только импорт (/rsk/import), больше никто и никогда.
#   слой 2 (отработка, изменяемый людьми) — rsk_processing + m2m
#     (rsk_processing_responsible); пишет только форма /rsk/processing.
#     Повторный импорт акта слой 2 не трогает.
#
# Три независимых трека — Физика/Проект/ИД, каждый со своим набором
# значений, не единая категория+статус (откат 07.09.2026: замена на
# категорию+статус, миграция 025, была самодеятельностью — координатор
# не согласовывал схлопывание, см. KNOWN_ISSUES). "Факт" осмысленно
# только у track_phys (работы выполнены с отступлением от проекта,
# нужна корректировка РД — задача для ДПР, не синоним "выполнено") —
# поэтому у track_design/track_id в справочнике его нет вовсе.
RU_RSK_TRACK = {
    "not_required": "не треб.", "not_done": "не вып.", "done": "вып.",
    "fact": "факт (нужна корр. РД)", "unknown": "—",
}

# "Латералим" последнюю по акту позицию каждого нарушения — от акта к
# акту формулировка может меняться, актуальная = из последнего акта, где
# нарушение встречается (докс, "текст живёт в позиции акта").
RSK_LIST_BASE_SQL = """
    from rsk_violation v
    join lateral (
        select i.* from rsk_act_item i where i.violation_id = v.id order by i.act_id desc limit 1
    ) i on true
    join rsk_act a on a.id = i.act_id
    left join rsk_act ca on ca.id = v.closed_in_act_id
    left join rsk_processing p on p.violation_id = v.id
    left join rsk_close_condition cc on cc.id = p.close_condition_id
"""

RSK_LIST_SELECT_SQL = f"""
    select v.id, v.sys_no, v.is_active, v.first_act_no, v.first_detected_date,
           i.content, i.remedy, i.due_date, i.is_repeat, i.control_section,
           a.act_no, a.act_date,
           ca.act_date as closed_act_date,
           coalesce(p.track_phys, 'unknown') as track_phys,
           coalesce(p.track_design, 'unknown') as track_design,
           coalesce(p.track_id, 'unknown') as track_id,
           coalesce(p.rejected, false) as rejected,
           p.id as processing_id, p.planned_close_date, p.comment as processing_comment,
           cc.label as close_condition_label,
           coalesce(
               (select string_agg(r.name, ', ' order by r.name)
                from rsk_processing_responsible pr join rsk_responsible r on r.id = pr.responsible_id
                where pr.processing_id = p.id),
               '—'
           ) as responsible_names
    {RSK_LIST_BASE_SQL}
"""


def rsk_pseudo_status(row):
    """Статус для отображения — вычисляется, не хранится (докс: "статусы
    из [комментария] не выводить"; здесь то же самое, но из треков)."""
    if not row["is_active"]:
        return "closed"
    if row["rejected"]:
        return "rejected"
    if row["track_phys"] in ("done", "not_required") and row["track_design"] in ("done", "not_required") \
            and row["track_id"] in ("done", "not_required"):
        return "ready"
    if row["track_phys"] == "fact":
        return "needs_rd"
    return "open"


RU_RSK_STATUS = {
    "closed": "Снято", "rejected": "Отклонено РСК", "ready": "Готово к снятию",
    "needs_rd": "Ждёт корректировки РД", "open": "В работе",
}
RSK_STATUS_BADGE = {
    "closed": "badge-ok", "rejected": "badge-bad", "ready": "badge-ok",
    "needs_rd": "badge-warn", "open": "badge-neutral",
}


@app.get("/rsk")
def rsk_registry_page(request: Request, responsible: str = "", track_phys: str = "",
                       track_design: str = "", track_id_: str = "", act_no: str = "",
                       is_repeat: str = "", state: str = ""):
    where = ["1=1"]
    params = []
    if responsible.strip():
        where.append(
            "p.id is not null and exists (select 1 from rsk_processing_responsible pr3 "
            "where pr3.processing_id = p.id and pr3.responsible_id = %s)"
        )
        params.append(int(responsible))
    if track_phys.strip():
        where.append("coalesce(p.track_phys, 'unknown') = %s")
        params.append(track_phys)
    if track_design.strip():
        where.append("coalesce(p.track_design, 'unknown') = %s")
        params.append(track_design)
    if track_id_.strip():
        where.append("coalesce(p.track_id, 'unknown') = %s")
        params.append(track_id_)
    if act_no.strip():
        where.append("a.act_no = %s")
        params.append(act_no.strip())
    if is_repeat in ("1", "0"):
        where.append("i.is_repeat = %s")
        params.append(is_repeat == "1")
    if state == "active":
        where.append("v.is_active")
    elif state == "closed":
        where.append("not v.is_active")

    rows = query(RSK_LIST_SELECT_SQL + " where " + " and ".join(where) + " order by v.sys_no desc", tuple(params))
    for r in rows:
        r["status_key"] = rsk_pseudo_status(r)

    responsibles = query("select id, name from rsk_responsible order by id")
    acts = query("select distinct act_no from rsk_act order by act_no desc")

    return render(request, "rsk_registry.html", "rsk-registry",
                  rows=rows, total=len(rows), responsibles=responsibles, acts=acts,
                  ru_track=RU_RSK_TRACK, ru_status=RU_RSK_STATUS, status_badge=RSK_STATUS_BADGE,
                  f_responsible=responsible, f_track_phys=track_phys, f_track_design=track_design,
                  f_track_id=track_id_, f_act_no=act_no, f_is_repeat=is_repeat, f_state=state)


@app.get("/export/rsk.csv")
def export_rsk_csv():
    rows = query(RSK_LIST_SELECT_SQL + " order by v.sys_no")
    out = [
        (r["sys_no"], RU_RSK_STATUS.get(rsk_pseudo_status(r), ""), r["content"], r["responsible_names"],
         RU_RSK_TRACK.get(r["track_phys"], ""), RU_RSK_TRACK.get(r["track_design"], ""),
         RU_RSK_TRACK.get(r["track_id"], ""),
         r["act_no"], _csv_dmy(r["act_date"]), _csv_dmy(r["due_date"]), _csv_dmy(r["closed_act_date"]),
         "да" if r["is_repeat"] else "нет")
        for r in rows
    ]
    return _csv_response(
        "rsk_registry.csv",
        ["№", "Статус", "Содержание", "Ответственные", "Физика", "Проект", "ИД",
         "Акт", "Проверка", "Срок", "Устранено", "Повторно"],
        out,
    )


# Общие цифры РСК — источник для /rsk/dashboard и для блока "Обзор РСК"
# на главном дашборде (координатор, 06.09.2026: РСК теперь равноправный
# раздел меню рядом с СМР/ИД, одной плитки внутри блока ИД недостаточно).
# Вынесено в функцию, чтобы не дублировать SQL между двумя местами.
def compute_rsk_dashboard_stats():
    tiles = query_one(f"""
        select
            count(*) filter (where v.is_active) as total_active,
            count(*) filter (where v.is_active and coalesce(p.track_phys,'unknown') in ('done','not_required')
                and coalesce(p.track_design,'unknown') in ('done','not_required')
                and coalesce(p.track_id,'unknown') in ('done','not_required')) as ready_to_close,
            count(*) filter (where v.is_active and cc.code = 'id_priniatie') as blocked_by_id,
            count(*) filter (where v.is_active and coalesce(p.track_phys,'unknown') = 'fact') as needs_rd,
            count(*) filter (where v.is_active and coalesce(p.rejected, false)) as rejected
        {RSK_LIST_BASE_SQL}
    """) or {}

    by_responsible = query("""
        select r.id, r.name, count(*) as n
        from rsk_processing_responsible pr
        join rsk_responsible r on r.id = pr.responsible_id
        join rsk_processing p on p.id = pr.processing_id
        join rsk_violation v on v.id = p.violation_id and v.is_active
        group by r.id, r.name order by n desc
    """)
    no_responsible = query_one(f"""
        select count(*) as n {RSK_LIST_BASE_SQL} where v.is_active and p.id is null
    """) or {"n": 0}

    return {"tiles": tiles, "by_responsible": by_responsible, "no_responsible": no_responsible["n"]}


@app.get("/rsk/dashboard")
def rsk_dashboard_page(request: Request):
    stats = compute_rsk_dashboard_stats()
    return render(request, "rsk_dashboard.html", "rsk-dashboard",
                  tiles=stats["tiles"], by_responsible=stats["by_responsible"],
                  no_responsible=stats["no_responsible"])


@app.get("/rsk/violation/{sys_no}")
def rsk_violation_detail(request: Request, sys_no: int):
    v = query_one("select * from rsk_violation where sys_no=%s", (sys_no,))
    if not v:
        return RedirectResponse(url="/rsk", status_code=303)
    items = query(
        "select i.*, a.act_no, a.act_date from rsk_act_item i join rsk_act a on a.id=i.act_id "
        "where i.violation_id=%s order by a.act_date desc, a.id desc",
        (v["id"],),
    )
    latest = items[0] if items else None
    processing = query_one("select * from rsk_processing where violation_id=%s", (v["id"],))
    responsible_ids = set()
    if processing:
        responsible_ids = {
            r["responsible_id"] for r in
            query("select responsible_id from rsk_processing_responsible where processing_id=%s",
                  (processing["id"],))
        }
    close_conditions = query("select id, label from rsk_close_condition order by id")
    responsibles = query("select id, name from rsk_responsible order by id")

    return render(request, "rsk_detail.html", "rsk-registry",
                  v=v, items=items, latest=latest, processing=processing,
                  responsible_ids=responsible_ids,
                  close_conditions=close_conditions, responsibles=responsibles,
                  ru_track=RU_RSK_TRACK,
                  status_key=rsk_pseudo_status({**(latest or {}), "is_active": v["is_active"],
                                                 "rejected": processing["rejected"] if processing else False,
                                                 "track_phys": processing["track_phys"] if processing else "unknown",
                                                 "track_design": processing["track_design"] if processing else "unknown",
                                                 "track_id": processing["track_id"] if processing else "unknown"}),
                  ru_status=RU_RSK_STATUS)


# ---------------------------------------------------------------------
# Форма «Отработка предписаний» — слой 2. Читает слой 1 (только для
# чтения — содержание/мероприятие), пишет только rsk_processing +
# её m2m. Один нарушение выбирается по sys_no (поле сверху или клик по
# строке реестра ниже — обычная перезагрузка страницы с ?sys_no=,
# без JS-подгрузки: инструмент для одного инженера ПТО, не нужен SPA).
# ---------------------------------------------------------------------

@app.get("/rsk/processing")
def rsk_processing_page(request: Request, sys_no: str = "", f_responsible: str = "",
                         f_track_phys: str = "", f_track_design: str = "", f_track_id: str = ""):
    v = None
    processing = None
    responsible_ids = set()
    if sys_no.strip():
        try:
            v = query_one("select * from rsk_violation where sys_no=%s", (int(sys_no),))
        except ValueError:
            v = None
        if v:
            latest_item = query_one(
                "select i.* from rsk_act_item i where i.violation_id=%s order by i.act_id desc limit 1",
                (v["id"],),
            )
            v["content"] = latest_item["content"] if latest_item else None
            v["remedy"] = latest_item["remedy"] if latest_item else None
            v["due_date"] = latest_item["due_date"] if latest_item else None
            processing = query_one("select * from rsk_processing where violation_id=%s", (v["id"],))
            if processing:
                responsible_ids = {
                    r["responsible_id"] for r in
                    query("select responsible_id from rsk_processing_responsible where processing_id=%s",
                          (processing["id"],))
                }

    where = ["1=1"]
    params = []
    if f_responsible.strip():
        where.append(
            "p.id is not null and exists (select 1 from rsk_processing_responsible pr3 "
            "where pr3.processing_id = p.id and pr3.responsible_id = %s)"
        )
        params.append(int(f_responsible))
    if f_track_phys.strip():
        where.append("coalesce(p.track_phys, 'unknown') = %s")
        params.append(f_track_phys)
    if f_track_design.strip():
        where.append("coalesce(p.track_design, 'unknown') = %s")
        params.append(f_track_design)
    if f_track_id.strip():
        where.append("coalesce(p.track_id, 'unknown') = %s")
        params.append(f_track_id)
    where.append("v.is_active")
    rows = query(RSK_LIST_SELECT_SQL + " where " + " and ".join(where) + " order by v.sys_no desc limit 200",
                 tuple(params))
    for r in rows:
        r["status_key"] = rsk_pseudo_status(r)

    responsibles = query("select id, name from rsk_responsible order by id")
    close_conditions = query("select id, label from rsk_close_condition order by id")

    return render(request, "rsk_processing.html", "rsk-processing",
                  v=v, processing=processing, responsible_ids=responsible_ids,
                  rows=rows, responsibles=responsibles, close_conditions=close_conditions,
                  ru_track=RU_RSK_TRACK, ru_status=RU_RSK_STATUS, status_badge=RSK_STATUS_BADGE,
                  f_responsible=f_responsible, f_track_phys=f_track_phys,
                  f_track_design=f_track_design, f_track_id=f_track_id)


@app.post("/api/rsk-processing")
def api_rsk_processing_upsert(
    request: Request, sys_no: int = Form(...),
    track_phys: str = Form("unknown"), track_design: str = Form("unknown"), track_id_: str = Form("unknown"),
    responsible_ids: list[int] = Form(default=[]), close_condition_id: str = Form(""),
    planned_close_date: str = Form(""), rejected: str = Form(""), comment: str = Form(""),
):
    if not has_permission(request.state.user, "rsk:submit"):
        return RedirectResponse(
            url=f"/rsk/processing?sys_no={sys_no}&err=" + urllib.parse.quote("Нет доступа к отработке предписаний РСК."),
            status_code=303,
        )
    v = query_one("select id from rsk_violation where sys_no=%s", (sys_no,))
    if not v:
        return RedirectResponse(url="/rsk/processing?err=" + urllib.parse.quote("Нарушение не найдено."),
                                 status_code=303)

    cc_val = int(close_condition_id) if close_condition_id.strip() else None
    planned_val = _parse_date(planned_close_date) if planned_close_date.strip() else None
    rejected_val = rejected == "1"
    comment_val = comment.strip() or None
    user_id = current_user_id_or_web_form()

    def _do(cur):
        cur.execute(
            """
            insert into rsk_processing
                (violation_id, track_phys, track_design, track_id, close_condition_id,
                 planned_close_date, rejected, comment, updated_ts)
            values (%s,%s,%s,%s,%s,%s,%s,%s, now())
            on conflict (violation_id) do update set
                track_phys=excluded.track_phys, track_design=excluded.track_design,
                track_id=excluded.track_id, close_condition_id=excluded.close_condition_id,
                planned_close_date=excluded.planned_close_date, rejected=excluded.rejected,
                comment=excluded.comment, updated_ts=now()
            returning id
            """,
            (v["id"], track_phys, track_design, track_id_, cc_val, planned_val, rejected_val, comment_val),
        )
        processing_id = cur.fetchone()["id"]

        cur.execute("delete from rsk_processing_responsible where processing_id=%s", (processing_id,))
        for rid in responsible_ids:
            cur.execute(
                "insert into rsk_processing_responsible (processing_id, responsible_id) values (%s,%s) "
                "on conflict do nothing",
                (processing_id, rid),
            )

        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'rsk_processing', %s, 'rsk_processing_update', %s, 'форма /rsk/processing')",
            (user_id, processing_id, json.dumps(
                {"sys_no": sys_no, "track_phys": track_phys, "track_design": track_design,
                 "track_id": track_id_}, ensure_ascii=False)),
        )
        return processing_id

    run_in_transaction(_do)
    ok_msg = urllib.parse.quote(f"Отработка нарушения №{sys_no} сохранена.")
    return RedirectResponse(url=f"/rsk/processing?sys_no={sys_no}&ok={ok_msg}", status_code=303)


# ---------------------------------------------------------------------
# Форма «Загрузка акта проверки» — единственный писатель слоя 1. Два
# шага: /rsk/import (форма + после отправки — предпросмотр с диффом,
# ничего ещё не записано) → /rsk/import/confirm (запись). Файл между
# шагами лежит в RSK_UPLOADS_DIR под токеном.
# ---------------------------------------------------------------------

def _rsk_diff_vs_previous(new_records):
    new_by_sysno = {r["sys_no"]: r for r in new_records}
    new_sysnos = set(new_by_sysno)

    prev_act = query_one("select id, act_no, act_date from rsk_act order by act_date desc, id desc limit 1")
    if not prev_act:
        return {"prev_act": None, "new": sorted(new_sysnos), "removed": [], "changed": [], "unchanged": []}

    prev_items = query(
        "select v.sys_no, i.content, i.remedy from rsk_act_item i "
        "join rsk_violation v on v.id = i.violation_id where i.act_id=%s",
        (prev_act["id"],),
    )
    prev_by_sysno = {r["sys_no"]: r for r in prev_items}
    prev_sysnos = set(prev_by_sysno)

    new_list = sorted(new_sysnos - prev_sysnos)
    removed_list = sorted(prev_sysnos - new_sysnos)
    changed_list = []
    unchanged_list = []
    for sn in sorted(new_sysnos & prev_sysnos):
        old = prev_by_sysno[sn]
        new = new_by_sysno[sn]
        if (old["content"] or "") != (new["content"] or "") or (old["remedy"] or "") != (new["remedy"] or ""):
            changed_list.append(sn)
        else:
            unchanged_list.append(sn)

    return {"prev_act": prev_act, "new": new_list, "removed": removed_list,
            "changed": changed_list, "unchanged": unchanged_list}


@app.get("/rsk/import")
def rsk_import_page(request: Request):
    return render(request, "rsk_import.html", "rsk-import", preview=None)


@app.post("/rsk/import")
def rsk_import_preview(request: Request, act_pdf: UploadFile = File(...)):
    if not has_permission(request.state.user, "rsk:submit"):
        return render(request, "rsk_import.html", "rsk-import", preview=None,
                      errors=["Нет доступа к загрузке актов РСК."])
    if not act_pdf.filename or not act_pdf.filename.lower().endswith(".pdf"):
        return render(request, "rsk_import.html", "rsk-import", preview=None,
                      errors=["Файл должен быть в формате PDF."])

    token = secrets.token_hex(8)
    pdf_path = os.path.join(RSK_UPLOADS_DIR, f"{token}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(act_pdf.file.read())

    try:
        parsed = parse_act(pdf_path)
    except Exception as e:  # noqa: BLE001 — предпросмотр, показать причину и дать переcкачать другой файл
        os.remove(pdf_path)
        return render(request, "rsk_import.html", "rsk-import", preview=None,
                      errors=[f"Не удалось разобрать PDF: {e}"])

    diff = _rsk_diff_vs_previous(parsed["records"])
    return render(request, "rsk_import.html", "rsk-import", preview=parsed, diff=diff, token=token,
                  original_name=act_pdf.filename)


@app.post("/rsk/import/confirm")
def rsk_import_confirm(request: Request, token: str = Form(...)):
    if not has_permission(request.state.user, "rsk:submit"):
        return RedirectResponse(url="/rsk/import?err=" + urllib.parse.quote("Нет доступа."), status_code=303)
    pdf_path = os.path.join(RSK_UPLOADS_DIR, f"{token}.pdf")
    if not os.path.exists(pdf_path):
        return RedirectResponse(
            url="/rsk/import?err=" + urllib.parse.quote("Файл предпросмотра не найден — загрузите заново."),
            status_code=303,
        )
    parsed = parse_act(pdf_path)
    diff = _rsk_diff_vs_previous(parsed["records"])

    def _do(cur):
        act = parsed["act"]
        cur.execute(
            "insert into rsk_act (act_no, act_date, total_declared, pdf_path) values (%s,%s,%s,%s) "
            "on conflict (act_no) do update set act_date=excluded.act_date, "
            "total_declared=excluded.total_declared returning id",
            (act["act_no"], act["act_date"], act["total_declared"], pdf_path),
        )
        act_id = cur.fetchone()["id"]

        for rec in parsed["records"]:
            cur.execute(
                """
                insert into rsk_violation (sys_no, first_act_no, first_detected_date, is_active, closed_in_act_id)
                values (%(sys_no)s, %(first_act_no)s, %(first_detected_date)s, true, null)
                on conflict (sys_no) do update set
                    first_detected_date = least(rsk_violation.first_detected_date, excluded.first_detected_date),
                    is_active = true, closed_in_act_id = null
                returning id
                """,
                rec,
            )
            violation_id = cur.fetchone()["id"]
            cur.execute(
                """
                insert into rsk_act_item
                    (act_id, violation_id, item_no, control_section, content, remedy, due_date, is_repeat)
                values (%(act_id)s, %(violation_id)s, %(item_no)s, %(control_section)s, %(content)s,
                        %(remedy)s, %(due_date)s, %(is_repeat)s)
                on conflict (act_id, violation_id) do update set
                    item_no=excluded.item_no, control_section=excluded.control_section,
                    content=excluded.content, remedy=excluded.remedy, due_date=excluded.due_date,
                    is_repeat=excluded.is_repeat
                """,
                {**rec, "act_id": act_id, "violation_id": violation_id},
            )

        # Диф — снятие: структурно, по множеству sys_no, никогда не по
        # разбору человеческого текста. Заход 3, 10.09.2026, задача 5:
        # закрываем РОВНО те sys_no, что уже показаны пользователю в
        # diff["removed"] на предпросмотре (main.py:6890) — раньше здесь
        # был отдельный, второй запрос активных нарушений, который мог
        # разойтись с "removed" предпросмотра, если между предпросмотром
        # и подтверждением что-то в rsk_violation изменилось (та же
        # болезнь, что искал аудит 08.09.2026, — то же число, посчитанное
        # дважды). Один источник: закрывается то, что показано.
        if diff["removed"]:
            cur.execute(
                "update rsk_violation set is_active=false, closed_in_act_id=%s where sys_no = any(%s)",
                (act_id, diff["removed"]),
            )

        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, new_value, reason) "
            "values (%s, 'rsk_act', %s, 'rsk_act_import', %s, 'форма /rsk/import')",
            (current_user_id_or_web_form(), act_id, json.dumps(
                {"act_no": act["act_no"], "positions": len(parsed["records"]),
                 "new": len(diff["new"]), "removed": len(diff["removed"]), "changed": len(diff["changed"])},
                ensure_ascii=False)),
        )
        return act_id

    act_id = run_in_transaction(_do)
    os.remove(pdf_path)
    ok_msg = urllib.parse.quote(
        f"Акт {parsed['act']['act_no']} загружен: {len(parsed['records'])} позиций, "
        f"{len(diff['new'])} новых, {len(diff['removed'])} снято."
    )
    return RedirectResponse(url=f"/rsk?ok={ok_msg}", status_code=303)
