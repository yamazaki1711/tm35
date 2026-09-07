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
            request, "form.html", "data",
            work_rows=work_rows, reason_codes=REASON_CODES,
            errors=errors, warnings=warnings, ok=False,
            values={
                "work_id": work_id, "date": date, "planned_crew": planned_crew,
                "actual_crew": actual_crew, "fact_pct": fact_pct,
                "plan_finish_date": plan_finish_date,
                "reason_code": reason_code, "comment": comment,
            },
        )

    user_id = ensure_web_form_user()

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
@app.get("/api/existing-entry-v2")
def api_existing_entry_v2(work_id: int, date: str):
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
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
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
    return render(request, "id_packages.html", "data",
                  packages=packages, stats=stats, total=total)


# ====== GET /changes — список ИЗМ ======
@app.get("/changes")
def changes_page(request: Request):
    rows = query(
        "select code, section_code, topic, status, designer_name, request_date, sla_days, "
        "planned_response_date, actual_response_date, overdue_days, escalation_level, blocked_amount_rub "
        "from change order by blocked_amount_rub desc nulls last, request_date nulls last"
    )
    total = len(rows) if rows else 0
    overdue = sum(1 for r in rows if r['overdue_days'] and r['overdue_days'] > 0) if rows else 0
    return render(request, "changes.html", "data",
                  changes=rows or [], total=total, overdue=overdue, errors=[], values={})


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
        return render(request, "changes.html", "data",
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

    execute_returning(
        """insert into change 
        (code, section_code, change_number, topic, description, initiator, status, designer_name,
         request_date, sla_days, planned_response_date, overdue_days, blocked_amount_rub,
         request_file_url, comment)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id""",
        (code_val, section_val, num_val, topic.strip(), desc_val, initiator, status, designer_val,
         req_date_val, sla_val, plan_resp_val, overdue_val, amt_val, url_val, comment_val)
    )
    return RedirectResponse(url="/changes?ok=1", status_code=303)


# ====== GET /prescriptions — список предписаний ======
@app.get("/prescriptions")
def prescriptions_page(request: Request):
    rows = query(
        "select code, source, document_number, document_date, category, area, description, "
        "required_action, due_date, status, amount_unblocked "
        "from prescription order by status, document_date desc nulls last limit 300"
    )
    total = len(rows) if rows else 0
    return render(request, "prescriptions.html", "data",
                  prescriptions=rows or [], total=total, errors=[], values={})


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
        return render(request, "prescriptions.html", "data",
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

    execute_returning(
        """insert into prescription
        (code, source, document_number, document_date, category, area, description,
         required_action, due_date, status, amount_unblocked, document_url, comment)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
        returning id""",
        (code_val, source, doc_num_val, doc_date_val, cat_val, area_val, description.strip(),
         required_action, due_val, amt_val, url_val, comment_val)
    )
    return RedirectResponse(url="/prescriptions?ok=1", status_code=303)
