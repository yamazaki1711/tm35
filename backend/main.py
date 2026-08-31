import contextvars
import hashlib
import json
import os
import re
import secrets
import urllib.parse
from datetime import date as date_cls, datetime as datetime_cls, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from db import query, query_one, execute, run_in_transaction
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
    if "zone:id" in perms and (permission.startswith("id_tab:") or permission in ("changes:submit", "prescriptions:submit")):
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
    return templates.TemplateResponse(request, template, {"active": active, **ctx})


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

    works = query("select id, fact_pct, status from work")
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
    Общая для / и /critical выборка: критичность отставания, прогноз
    завершения, ресурсный дефицит. Формулы и обоснование — backend/analytics.py
    и docs/GAP_ANALYSIS.md (Цикл 1). Использует ТОЛЬКО данные из БД —
    ничего не придумывает; если временного baseline нет для работы, она
    просто не попадает в расчёт (не считается ни просроченной, ни в срок).
    """
    today = object_today()

    works_with_baseline = query(
        """
        select w.code, w.name, w.status, bs.plan_finish
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
    forecast_date, avg_lag, baseline_date = compute_project_forecast(active_finishes, overdue_lags)

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
    гистограмма численности по дням, две независимые оценки прогноза
    (по темпу и по baseline+просрочке), тренд прогноза по неделям,
    дефицит ресурса до директивного срока.
    """
    today = object_today()
    evm = get_evm_data()
    crit = get_criticality_data()
    remaining, known_count, excluded_count = get_remaining_effort()

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

    forecast_pace_date = avg_pace = None
    if evm.get("available"):
        recent_rows = query(
            LATEST_DP_CTE + """
            select date, sum(actual_crew) as v from latest_dp
            where actual_crew is not null
            group by date order by date desc limit 14
            """
        )
        recent_actuals = [float(r["v"] or 0) for r in recent_rows]
        forecast_pace_date, avg_pace, _ = compute_forecast_by_pace(remaining, recent_actuals, today)
        record_forecast_snapshot(today, forecast_pace_date, "pace", round(remaining, 1), avg_pace)
    if crit.get("forecast_date"):
        record_forecast_snapshot(today, crit["forecast_date"], "baseline_lag", None, None)

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
        """
        select w.id, w.code, w.name, w.location, w.executor_type, w.subcontractor_id, bs.plan_start
        from work w
        join baseline_schedule bs on bs.work_id = w.id
        where bs.plan_start between %s and %s
          and bs.confidence in ('high', 'medium')
          and w.status = 'not_started'
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
        """
        select w.id, w.code, w.name, w.fact_pct, w.status, bs.plan_finish
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
    result = []
    for w in works:
        if w["status"] in DONE_STATUSES:
            continue
        reasons = []
        if w["plan_finish"] < today:
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
    sql = "select code, source, location, name, unit, status, executor_type, fact_pct, fact_pct_raw, data_quality_flag from work where true"
    params = []
    if source:
        sql += " and source=%s"; params.append(source)
    if status:
        sql += " and status=%s"; params.append(status)
    if executor_type:
        sql += " and executor_type=%s"; params.append(executor_type)
    if q:
        sql += " and name ilike %s"; params.append(f"%{q}%")
    sql += " order by code"
    rows = query(sql, params)

    sources = query("select distinct source from work order by source")
    statuses = query("select distinct status from work order by status")

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
    return render(request, "gantt.html", "gantt", can_write=can_write)


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
            start_date = object_today() - timedelta(days=7)
    else:
        start_date = object_today() - timedelta(days=7)
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
        where_extra += " and w.status not in %s"
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
        select w.id, w.code, w.name, w.unit, w.volume, w.fact_pct, w.status,
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
    overdue_codes = {w["code"] for w in compute_overdue(
        query(
            """
            select w.code, w.name, w.status, bs.plan_finish
            from work w join baseline_schedule bs on bs.work_id = w.id
            where bs.plan_finish is not null
            """
        ),
        object_today(),
    )}

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

    day_blockers_rows = query(
        "select date(created_at) as d, blocker_type, count(*) as n from blocker "
        "where work_id is null and created_at::date between %s and %s "
        "group by date(created_at), blocker_type",
        (start_date, end_date),
    )
    day_blockers = {}
    for r in day_blockers_rows:
        day_blockers.setdefault(r["d"].isoformat(), []).append(r["blocker_type"])

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
            # прежнему критерию "не завершена физически".
            where_extra += " and w.status not in %s"
            params.append(tuple(DONE_STATUSES))
    if q.strip():
        where_extra += " and (w.code ilike %s or w.name ilike %s)"
        like = f"%{q.strip()}%"
        params += [like, like]

    works = query(
        f"""
        select w.id, w.code, w.name, w.location, w.source, w.status, w.fact_pct as work_fact_pct
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
    # не всегда весь реестр целиком.
    sql = ("select code, source, location, name, unit, status, executor_type, "
           "fact_pct, plan_finish_date from work where true")
    params = []
    if source:
        sql += " and source=%s"; params.append(source)
    if status:
        sql += " and status=%s"; params.append(status)
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
    rows = query(
        "select seq_no, section_no, location, composition, amount_no_vat, status_code, status_formation "
        "from id_package order by seq_no"
    )
    S_LABELS = {"S00": "Черновик", "S10": "Сформирован", "S20": "В РСК", "S40": "Заблокирован ИЗМ",
                "S60": "Подписан РСК", "S80": "В СДО", "S90": "Закрыт в КС-2"}
    out = [
        (r["seq_no"], r["section_no"], r["location"], r["composition"], r["amount_no_vat"],
         S_LABELS.get(r["status_code"], r["status_code"] or "Черновик"), r["status_formation"])
        for r in rows
    ]
    return _csv_response(
        "id_packages.csv",
        ["№", "Раздел", "Участок", "Состав", "Сумма без НДС", "Статус", "Статус формирования (источник)"],
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


@app.get("/export/prescriptions.csv")
def export_prescriptions_csv():
    rows = query(
        "select code, source, document_number, document_date, category, area, description, "
        "required_action, due_date, status, amount_unblocked "
        "from prescription order by status, document_date desc nulls last"
    )
    out = [
        (r["code"], r["source"], r["document_number"], _csv_dmy(r["document_date"]),
         r["category"], r["area"], r["description"], r["required_action"],
         _csv_dmy(r["due_date"]), r["status"], r["amount_unblocked"])
        for r in rows
    ]
    return _csv_response(
        "prescriptions.csv",
        ["Код", "Источник", "№ документа", "Дата документа", "Категория", "Участок", "Описание",
         "Требуемое действие", "Срок", "Статус", "Разблокировано, ₽"],
        out,
    )


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


# ====== GET /id-packages — список пакетов ИД ======
@app.get("/id-packages")
def id_packages_page(request: Request):
    packages = query(
        "select seq_no, section_no, location, composition, amount_no_vat, status_formation, status_code, "
        "date_s10_formed, date_s20_to_rsk, date_s60_signed, date_s90_closed_ks, drive_folder_url "
        "from id_package order by seq_no limit 500"
    )
    stats_rows = query("select status_code, count(*) as cnt from id_package group by status_code")
    stats = {r['status_code']: r['cnt'] for r in stats_rows} if stats_rows else {}
    total = sum(stats.values()) if stats else 0
    return render(request, "id_packages.html", "id-packages",
                  packages=packages, stats=stats, total=total)


# ====== GET /changes — список ИЗМ ======
@app.get("/changes")
def changes_page(request: Request):
    rows = query(
        "select id, code, section_code, topic, status, designer_name, request_date, sla_days, "
        "planned_response_date, actual_response_date, overdue_days, escalation_level, blocked_amount_rub "
        "from change order by blocked_amount_rub desc nulls last, request_date nulls last"
    )
    total = len(rows) if rows else 0
    overdue = sum(1 for r in rows if r['overdue_days'] and r['overdue_days'] > 0) if rows else 0
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
    initiator: str = Form("RSK"),
    status: str = Form("DRAFT"),
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

    if errors:
        rows = query(
            "select code, section_code, topic, status, designer_name, request_date, sla_days, "
            "planned_response_date, actual_response_date, overdue_days, escalation_level, blocked_amount_rub "
            "from change order by blocked_amount_rub desc nulls last"
        )
        total = len(rows) if rows else 0
        overdue = sum(1 for r in rows if r['overdue_days'] and r['overdue_days'] > 0) if rows else 0
        return render(request, "changes.html", "changes",
                      changes=rows or [], total=total, overdue=overdue,
                      errors=errors, values={
                          "code": code, "section_code": section_code, "change_number": change_number,
                          "topic": topic, "description": description, "initiator": initiator,
                          "status": status, "designer_name": designer_name, "request_date": request_date,
                          "sla_days": sla_days, "blocked_amount_rub": blocked_amount_rub,
                          "request_file_url": request_file_url, "comment": comment,
                      })

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
    run_in_transaction(_insert_change)
    return RedirectResponse(url="/changes?ok=1", status_code=303)


# ====== GET /prescriptions — список предписаний ======
@app.get("/prescriptions")
def prescriptions_page(request: Request):
    rows = query(
        "select id, code, source, document_number, document_date, category, area, description, "
        "required_action, due_date, status, amount_unblocked "
        "from prescription order by status, document_date desc nulls last limit 300"
    )
    total = len(rows) if rows else 0
    can_edit = has_permission(request.state.user, "prescriptions:submit")
    return render(request, "prescriptions.html", "prescriptions",
                  prescriptions=rows or [], total=total, errors=[], values={}, can_edit=can_edit)


@app.post("/api/prescription/{prescription_id}/status")
def api_prescription_update_status(request: Request, prescription_id: int, status: str = Form(...)):
    # Учётные записи, 29.08.2026: тот же пробел, что у ИЗМ — только
    # создание, редактирования не было. Достроено.
    if not has_permission(request.state.user, "prescriptions:submit"):
        return JSONResponse({"ok": False, "error": "Нет доступа к форме предписаний."}, status_code=403)
    row = query_one("select id, status from prescription where id=%s", (prescription_id,))
    if not row:
        return JSONResponse({"ok": False, "error": "Запись не найдена."}, status_code=404)

    user_id = current_user_id_or_web_form()
    close_val = object_today() if status == "CLOSED" else None

    def _do(cur):
        cur.execute("select status from prescription where id=%s for update", (prescription_id,))
        old_status = cur.fetchone()["status"]
        cur.execute(
            "update prescription set status=%s, actual_close_date=coalesce(%s, actual_close_date), "
            "updated_at=now(), updated_by=%s where id=%s",
            (status, close_val, user_id, prescription_id),
        )
        cur.execute(
            "insert into audit_log (user_id, entity_type, entity_id, action, old_value, new_value, reason) "
            "values (%s, 'prescription', %s, 'status_update', %s, %s, 'форма /prescriptions')",
            (user_id, prescription_id, json.dumps({"status": old_status}), json.dumps({"status": status})),
        )
    run_in_transaction(_do)
    return RedirectResponse(url="/prescriptions?ok=1", status_code=303)


# ====== POST /prescriptions — добавить предписание ======
@app.post("/prescriptions")
def prescriptions_post(
    request: Request,
    code: str = Form(""),
    source: str = Form("RSK"),
    document_number: str = Form(""),
    document_date: str = Form(""),
    category: str = Form(""),
    area: str = Form(""),
    description: str = Form(""),
    required_action: str = Form("TECH_SOLUTION"),
    due_date: str = Form(""),
    amount_unblocked: str = Form(""),
    document_url: str = Form(""),
    comment: str = Form(""),
):
    errors = []
    # Права по веткам, 30.08.2026 — та же находка, что у changes_post:
    # создание предписания не проверяло права вообще (только смена
    # статуса, api_prescription_update_status, была защищена).
    if not has_permission(request.state.user, "prescriptions:submit"):
        errors.append("Нет доступа к форме предписаний.")
    if not description.strip():
        errors.append("Описание обязательно.")

    code_val = code.strip() or None
    doc_num_val = document_number.strip() or None
    cat_val = category.strip() or None
    area_val = area.strip() or None
    url_val = document_url.strip() or None
    comment_val = comment.strip() or None

    # document_date
    doc_date_val = None
    if document_date.strip():
        doc_date_val = _parse_date(document_date)
        if not doc_date_val:
            errors.append("Дата документа указана некорректно.")

    # due_date
    due_val = None
    if due_date.strip():
        due_val = _parse_date(due_date)
        if not due_val:
            errors.append("Срок устранения указан некорректно.")

    # amount_unblocked
    amt_val = None
    if amount_unblocked.strip():
        try:
            amt_val = float(amount_unblocked.replace(',', '.'))
        except ValueError:
            errors.append("Сумма указана некорректно.")

    if errors:
        rows = query(
            "select code, source, document_number, document_date, category, area, description, "
            "required_action, due_date, status, amount_unblocked "
            "from prescription order by status, document_date desc nulls last"
        )
        total = len(rows) if rows else 0
        return render(request, "prescriptions.html", "prescriptions",
                      prescriptions=rows or [], total=total,
                      errors=errors, values={
                          "code": code, "source": source, "document_number": document_number,
                          "document_date": document_date, "category": category, "area": area,
                          "description": description, "required_action": required_action,
                          "due_date": due_date, "amount_unblocked": amount_unblocked,
                          "document_url": document_url, "comment": comment,
                      })

    # Auto-generate code
    if not code_val:
        prefix = source
        next_id = query_one("select coalesce(max(id),0)+1 as next from prescription")
        code_val = f"{prefix}-{next_id['next']:03d}"

    def _insert_prescription(cur):
        cur.execute(
            """insert into prescription
            (code, source, document_number, document_date, category, area, description,
             required_action, due_date, status, amount_unblocked, document_url, comment)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
            returning id""",
            (code_val, source, doc_num_val, doc_date_val, cat_val, area_val, description.strip(),
             required_action, due_val, amt_val, url_val, comment_val)
        )
        return cur.fetchone()['id']
    run_in_transaction(_insert_prescription)
    return RedirectResponse(url="/prescriptions?ok=1", status_code=303)


# ====== Обновлённый GET /dashboard (home) с передачей статистики новых разделов ======
@app.get("/dashboard")
def home_v2(request: Request):
    works_total = query_one("select count(*) as n from work")["n"]
    by_status = query(
        "select status, count(*) as n from work group by status order by n desc"
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

    # Новые статистики для карточек навигации
    id_stats_row = query_one("""
        select 
            count(*) as total,
            count(*) filter (where status_code = 'S60' or date_s60_signed is not null) as signed,
            count(*) filter (where status_code = 'S40' or date_s40_blocked is not null) as blocked
        from id_package
    """) or {"total": 0, "signed": 0, "blocked": 0}

    change_stats_row = query_one("""
        select 
            count(*) as total,
            count(*) filter (where overdue_days is not null and overdue_days > 0) as overdue
        from change
        where status not in ('INCLUDED_IN_RD', 'ARCHIVED')
    """) or {"total": 0, "overdue": 0}

    presc_stats_row = query_one("""
        select 
            count(*) as total,
            count(*) filter (where status = 'OPEN') as open
        from prescription
    """) or {"total": 0, "open": 0}

    crit = get_criticality_data()
    evm = get_evm_data()

    return render(
        request, "home.html", "dashboard",
        works_total=works_total, by_status=by_status,
        needs_review=needs_review, unresolved=unresolved,
        avg_pct=avg_pct, last_actual_date=last_actual_date,
        today_totals=today_totals, top_comments=top_comments,
        people_deficit=people_deficit, people_surplus=people_surplus,
        blockers_total=blockers_total, subcontractors_total=subcontractors_total,
        blockers_active_total=blockers_active_total, blockers_active=blockers_active,
        crit=crit, evm=evm,
        id_stats=id_stats_row,
        change_stats=change_stats_row,
        presc_stats=presc_stats_row,
    )



# =======================================================================
# Контур ИД — форма ввода по ответам ПТО (28.08.2026,
# TM35_ID_TZ_po_otvetam_PTO.md). Единица учёта — РАЗДЕЛ (строка
# вкладки), термин «папка» в интерфейсе не используется — папка
# появляется позже из нескольких подписанных разделов (id_package
# трогать не нужно, он остаётся отдельным старым импортом).
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


@app.get("/id-entry")
def id_entry_page(request: Request):
    tabs = query("select id, code, label, has_section_level, has_reference_block, note "
                 "from id_form_tab order by display_order")
    # Учётные записи, 29.08.2026: "заполняет форму только ответственный
    # за вкладку... чужие вкладки человек видеть в форме ввода не
    # должен". Применяется только к вошедшим НЕ-админам — анонимный
    # просмотр (сайт публичный) и координатор видят все вкладки; так же
    # не запираем координатора, если он забыл выдать себе разрешение на
    # конкретную вкладку.
    user = request.state.user
    if user and not is_admin(user):
        perms = user_permissions(user)
        # 30.08.2026: группа "zone:id" (вся ветка ИД) не должна попадать
        # под фильтр ниже — иначе видели бы 0 вкладок (нет отдельных
        # id_tab:xxx). Фильтруем только тех, у кого ЕСТЬ хотя бы одно
        # точечное разрешение (будущая точечная модель) — у кого нет ни
        # zone:id, ни отдельных id_tab:xxx (группа СМР, "только
        # просмотр"), видят все вкладки, как и анонимный посетитель —
        # не хуже публичного просмотра, писать всё равно не смогут
        # (проверка на POST /api/id-entry).
        id_tab_perms = {p for p in perms if p.startswith("id_tab:")}
        if "zone:id" not in perms and id_tab_perms:
            tabs = [t for t in tabs if f"id_tab:{t['code']}" in perms]
    return render(request, "id_entry.html", "id-entry", tabs=tabs)


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
def api_id_form_registry(tab_id: int = 0):
    where = "where e.tab_id=%s" if tab_id else ""
    params = (tab_id,) if tab_id else ()
    rows = query(
        f"""
        with latest as (
            select distinct on (row_id, work_type_id) *
            from id_form_entry
            order by row_id, work_type_id, created_at desc
        )
        select e.id, e.tab_id, t.label as tab_label, r.section_label, r.construction_label,
               wt.name as work_type_name, e.responsible_name, s.code as status_code,
               coalesce(s.label, 'статус не задан') as status_label,
               (s.id is null) as status_missing,
               s.is_stopper, e.status_date, e.planned_rsk_date,
               e.comment, e.created_at,
               b.id as block_id, b.change_ref, b.blocked_at
        from latest e
        join id_form_tab t on t.id = e.tab_id
        join id_form_row r on r.id = e.row_id
        left join id_form_work_type wt on wt.id = e.work_type_id
        left join id_form_status s on s.id = e.status_id
        left join id_form_block b on b.row_id = e.row_id
            and (b.work_type_id = e.work_type_id or (b.work_type_id is null and e.work_type_id is null))
            and b.unblocked_at is null
        {where}
        order by e.created_at desc
        limit 200
        """,
        params,
    )
    return {"rows": rows}


@app.post("/api/id-entry")
def api_id_entry_create(
    request: Request,
    tab_id: int = Form(...), row_id: int = Form(...), work_type_id: str = Form(""),
    responsible_name: str = Form(...), status_id: str = Form(""),
    status_date: str = Form(""), planned_rsk_date: str = Form(""),
    stop_factor: str = Form(""), comment: str = Form(""),
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

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    user_id = current_user_id_or_web_form()
    stop_val = stop_factor.strip() or None
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
                planned_rsk_date, blocker_id, comment, created_by)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (tab_id, row_id, wt_id_val, resp_val, status_id_val, status_date_val,
             planned_val, blocker_id_val, comment_val, user_id),
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
