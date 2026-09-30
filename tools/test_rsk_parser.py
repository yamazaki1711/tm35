#!/usr/bin/env python3
"""
Координатор, 25.09.2026 — тесты защит парсера акта РСК (rsk_parser.py):
скан без текстового слоя, реальный акт №4183-159 (должен по-прежнему
разбираться в то же число позиций, что уже лежит в базе), и акт со
сдвинутым форматом таблицы (защита `verify_column_header()`).

Ничего не пишет в БД — `parse_act()` сам по себе только читает PDF;
тест (б) читает БД (`rsk_violation`/`rsk_act`) для сверки ожидаемого
числа, ничего не меняет.

Тест (в) — не литеральный PDF-файл со сдвинутой вёрсткой: в контейнере
нет библиотеки для генерации PDF с текстовым слоем в произвольных
координатах (pypdfium2 — только чтение/растеризация, не запись текста;
добавлять новую зависимость ради одного теста — пересборка образа, вне
рамок этой правки). Вместо этого — прямой тест `verify_column_header()`
и `col_of()` на данных вида "как если бы" колонки печатались на других
x-координатах: та же функция, что видит настоящий PDF, с входом,
СОЗНАТЕЛЬНО сконструированным как сдвинутый акт. Ограничение
зафиксировано, не скрыто.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/test_rsk_parser.py [путь_к_реальному_акту.pdf]

Без аргумента тест (б) и растеризация в тесте (а) используют
/tmp/test_act.pdf, если он есть, иначе тест (б)/(а) пропускаются с
пометкой (не проваливаются) — сам PDF акта не хранится в репозитории и
не гарантирован на диске между сессиями.
"""
import sys

sys.path.insert(0, "/app")

import rsk_parser as rp  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    status = "ДА" if condition else "!! НЕТ !!"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def test_image_only(real_pdf_path):
    print("\n=== (а) Скан без текстового слоя ===")
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except ImportError as e:
        print(f"  ПРОПУЩЕН — {e}")
        return

    src = pdfium.PdfDocument(real_pdf_path)
    bitmap = src[0].render(scale=2.0)
    img = bitmap.to_pil()
    scan_path = "/tmp/_test_rsk_scan.pdf"
    img.save(scan_path, "PDF", resolution=150.0)

    result = rp.parse_act(scan_path)
    check("0 позиций разобрано", len(result["records"]) == 0, f"records={len(result['records'])}")
    check("номер акта не распознан", result["act"]["act_no"] is None)
    check("дата акта не распознана", result["act"]["act_date"] is None)
    # Порог — тот же RSK_TEXT_LAYER_MIN_CHARS, что main.py использует
    # для реального отказа (не отдельное число теста).
    sys.path.insert(0, "/app")
    import main as m
    check("total_chars_extracted ниже порога скана",
          result["checks"]["total_chars_extracted"] < m.RSK_TEXT_LAYER_MIN_CHARS,
          f"chars={result['checks']['total_chars_extracted']}")
    check("header_ok = False (нет текста — нет и сигнатуры колонок)",
          result["checks"]["header_ok"] is False)

    import os
    os.remove(scan_path)


def test_real_act(real_pdf_path):
    print("\n=== (б) Реальный акт №4183-159 ===")
    result = rp.parse_act(real_pdf_path)
    check("акт распознан как 4183-159", result["act"]["act_no"] == "4183-159",
          f"act_no={result['act']['act_no']}")
    check("дата акта — 2026-08-11", result["act"]["act_date"] == "2026-08-11",
          f"act_date={result['act']['act_date']}")
    check("header_ok = True (формат совпадает)", result["checks"]["header_ok"] is True)
    check("текстовый слой явно выше порога скана",
          result["checks"]["total_chars_extracted"] > 10000,
          f"chars={result['checks']['total_chars_extracted']}")

    try:
        sys.path.insert(0, "/app")
        import main as m
        db_count = m.query_one("select count(*) as n from rsk_violation")["n"]
        db_act = m.query_one("select act_no from rsk_act where act_no=%s", (result["act"]["act_no"],))
        check(f"число позиций совпадает с БД ({db_count})",
              len(result["records"]) == db_count,
              f"parsed={len(result['records'])}, db={db_count}")
        check("акт с этим номером действительно есть в БД (сверка, не гадание)",
              db_act is not None)
    except Exception as e:  # noqa: BLE001 — сверка с БД необязательна для этого теста
        print(f"  (сверка с БД пропущена: {e})")


def test_image_only_page_detection():
    """Координатор, 30.09.2026 — регрессия на находку №354 (act 4183-162):
    последняя страница акта была вложенным изображением без текстового
    слоя, и целая позиция (с итоговой строкой «Общее количество
    нарушений») выпала из разбора без единого сообщения — только
    общий счётчик total_chars_extracted (весь документ), который эту
    находку не ловит, поскольку 46 страниц из 47 читались нормально.

    Не литеральный PDF (см. докстринг теста (в) выше про отсутствие
    библиотеки для генерации PDF с произвольным содержимым в
    контейнере) — прямой тест `find_image_only_pages()` на фейковых
    страницах, воспроизводящих ровно то, что показал `page.objects` для
    настоящей страницы 46: 0 слов, есть изображение."""
    print("\n=== (г) Обнаружение страницы-изображения без текстового слоя ===")

    class FakePage:
        def __init__(self, words, n_images):
            self._words = words
            self.images = list(range(n_images))

        def extract_words(self):
            return self._words

    class FakePdf:
        pass

    normal_word = [{"text": "х", "top": 100.0, "x0": 60.0}]

    # Титульный лист (стр. 0) без слов и без картинки — не должен
    # попадать в список (иначе каждый акт с пустой стр. 0 отклонялся бы).
    pdf_titlepage_blank = FakePdf()
    pdf_titlepage_blank.pages = [FakePage([], 0), FakePage(normal_word, 0), FakePage(normal_word, 0)]
    check("пустая титульная страница (стр. 0) без картинки — не флагуется",
          rp.find_image_only_pages(pdf_titlepage_blank) == [])

    # Ровно сценарий №354: страницы 0-1 нормальные, страница 2 — 0 слов
    # + 1 изображение (как настоящая стр. 46 акта 4183-162).
    pdf_like_354 = FakePdf()
    pdf_like_354.pages = [FakePage(normal_word, 0), FakePage(normal_word, 0), FakePage([], 1)]
    check("страница с 0 слов и 1 изображением — флагуется",
          rp.find_image_only_pages(pdf_like_354) == [2],
          f"image_only_pages={rp.find_image_only_pages(pdf_like_354)}")

    # Контроль: страница без слов, но и без изображения (просто пустая
    # содержательная страница, например разделитель раздела) — НЕ флагуется,
    # это осознанное ограничение критерия (картинка — единственный сигнал,
    # отличающий «текст пропал» от «страница легитимно пуста»).
    pdf_blank_no_image = FakePdf()
    pdf_blank_no_image.pages = [FakePage(normal_word, 0), FakePage([], 0)]
    check("страница без слов и без картинки — не флагуется (нет сигнала отличить от легитимно пустой)",
          rp.find_image_only_pages(pdf_blank_no_image) == [])


def test_declared_vs_parsed_guard():
    """Координатор, 30.09.2026 — вторая защита той же находки: акт сам
    печатает «Общее количество нарушений - N» — если N не совпадает с
    числом реально разобранных позиций, это тоже сигнал, что часть акта
    не распознана (независимо от того, есть ли явная image_only-страница
    — например если недочитанная страница ЧАСТИЧНО извлекается, но не
    полностью). Тест — на самой функции _rsk_hard_guard_errors из
    main.py, с синтетическим parsed (без реального PDF)."""
    print("\n=== (д) Расхождение «заявлено / разобрано» ===")
    sys.path.insert(0, "/app")
    import main as m

    def fake_parsed(total_declared, n_records, image_only_pages=None):
        return {
            "act": {"act_no": "4183-000", "act_date": "2026-01-01"},
            "records": [{}] * n_records,
            "checks": {
                "total_chars_extracted": 50000,
                "header_ok": True,
                "total_declared_in_act": total_declared,
                "image_only_pages": image_only_pages or [],
            },
        }

    errs_mismatch = m._rsk_hard_guard_errors(fake_parsed(136, 135))
    check("136 заявлено / 135 разобрано — отклонено с понятным сообщением",
          len(errs_mismatch) == 1 and "136" in errs_mismatch[0] and "135" in errs_mismatch[0],
          f"errors={errs_mismatch}")

    errs_ok = m._rsk_hard_guard_errors(fake_parsed(135, 135))
    check("135 заявлено / 135 разобрано — проходит (совпадение)",
          errs_ok == [], f"errors={errs_ok}")

    errs_unknown = m._rsk_hard_guard_errors(fake_parsed(None, 135))
    check("итоговая строка не найдена (None) — не блокирует сама по себе",
          errs_unknown == [], f"errors={errs_unknown}")

    errs_image = m._rsk_hard_guard_errors(fake_parsed(135, 135, image_only_pages=[46]))
    check("image_only_pages непустой — отклонено даже при совпадении счёта",
          len(errs_image) == 1 and "47" in errs_image[0],
          f"errors={errs_image}")


def test_boilerplate_stripping():
    """Координатор, 30.09.2026 (продолжение находки №354, ч. 2) —
    базовая проверка _rsk_strip_boilerplate(): типовые фразы из
    RSK_BOILERPLATE_PHRASES убираются, содержательный текст остаётся."""
    print("\n=== (е) Чистка типовых фраз ===")
    sys.path.insert(0, "/app")
    import main as m

    text_with_boilerplate = m.norm_literal(
        "У персонала, занятого на производстве работ, отсутствуют средства "
        "индивидуальной защиты (каски). Производятся работы по устройству "
        "теплоизоляции. Нарушены требования проектной документации: л. 14 шифр "
        "2020.069.3000-ТМ-ПОС-ПЗ, изм.6; п. 9.1.7, п. 9.1.28 - 9.1.33 СП "
        "48.13330.2019 «Организация строительства»; ч. 6 ст. 52 ГрК РФ."
    )
    stripped = m._rsk_strip_boilerplate(text_with_boilerplate)
    check("после чистки не осталось длинной цитаты СП 48.13330.2019",
          "48.13330.2019" not in stripped, f"stripped={stripped!r}")
    check("содержательная часть (каски) осталась",
          "каски" in stripped, f"stripped={stripped!r}")
    check("чистка строго укорачивает текст (не расширяет и не искажает)",
          len(stripped) < len(text_with_boilerplate))

    # Текст ЦЕЛИКОМ из типовых фраз — после чистки почти пусто;
    # вызывающий код (_rsk_closure_review_candidates) обязан откатиться
    # на неочищенный текст в этом случае, сама функция — только чистит.
    only_boilerplate = m.norm_literal("Производятся работы по " + m.RSK_BOILERPLATE_PHRASES[1])
    stripped_empty = m._rsk_strip_boilerplate(only_boilerplate)
    check("текст из одних типовых фраз — чистка оставляет < 5 слов (сигнал для отката)",
          len(stripped_empty.split()) < 5, f"stripped_empty={stripped_empty!r}")


def test_replay_act_4183_162_closure_policy():
    """Координатор, 30.09.2026 — реплей акта 4183-162 «из состояния до
    импорта» (пул кандидатов реконструирован: активные сейчас + закрытые
    именно актом 15, контент — из последнего act_item ДО акта 15) под
    новой политикой (Part 2 координаторского промпта): закрывать молча
    можно только когда resolved=true И после чистки от типовых фраз
    нет похожего (≥0.40) пункта в новом акте.

    ИСХОДНАЯ ГИПОТЕЗА координатора была «16 устранённых закроются
    молча, №354 — на проверку». ФАКТ (проверено на реальных текстах,
    см. run-лог 30.09.2026): все 16 «устранённых» имеют в НОВОМ акте
    хотя бы одного похожего соседа с score ≥ 0.40 ПОСЛЕ чистки — не
    из-за общих цитат норм (чистка их убирает исправно, см. тест выше),
    а из-за того, что сам объект содержит МНОГО содержательно похожих
    нарушений на разных участках («работы по армированию опор X без
    освидетельствования... шифр...; п. 9.1.7...» — тот же дефект,
    другой пикет, разными инспектор пишет практически одним и тем же
    языком). Пример: №331 (участок УТ15-УТ16, структура «УТ16») против
    нового п.2.10 (тот же участок, структура «КР3») — score 0.994,
    различаются практически только скобкой с кодом структуры. Это не
    брешь чистки типовых фраз, это реальная, содержательная похожесть.

    Итог: под новой политикой ВСЕ 17 (16 «устранённых» + №354) уходят
    в «Проверить перед снятием», НИ ОДНО не закрывается молча — то есть
    для ЭТОГО акта новая политика не сократила число проверок (в отличие
    от гипотезы), но полностью устранила риск немого закрытия (то, ради
    чего она вводилась). Зафиксировано как факт, а не подогнано под
    ожидание — «отрицательный результат гипотезы окончателен», решение
    о дальнейшей агрессивности матчинга (например, вырезать коды
    структур/пикетов) — координатору (REQUIRES COORDINATOR DECISION)."""
    print("\n=== (ж) Реплей акта 4183-162 (реальные данные, из состояния до импорта) ===")
    sys.path.insert(0, "/app")
    import main as m
    import rsk_parser as rp

    real_pdf_path = "/app/uploads/rsk_acts/confirmed/4183-162.pdf"
    import os
    if not os.path.exists(real_pdf_path):
        print(f"  ПРОПУЩЕН — файл {real_pdf_path} не найден на диске")
        return

    def pre_import_candidates():
        rows = m.query("""
            select v.id as violation_id, v.sys_no, i.content, i.control_section,
                   coalesce(p.resolved, false) as resolved
            from rsk_violation v
            join lateral (
                select content, control_section from rsk_act_item
                where violation_id = v.id and act_id != 15
                order by act_id desc limit 1
            ) i on true
            left join rsk_processing p on p.violation_id = v.id
            where v.is_active or v.closed_in_act_id = 15
        """)
        for r in rows:
            r["_norm"] = m.norm_literal(r["content"] or "")
        return rows

    parsed = rp.parse_act(real_pdf_path)
    orig_open_candidates = m._rsk_open_candidates
    m._rsk_open_candidates = pre_import_candidates
    try:
        result = m._rsk_build_import_result(parsed["records"])
    finally:
        m._rsk_open_candidates = orig_open_candidates

    review_sysnos = sorted(c["violation"]["sys_no"] for c in result["closure_review"])
    check("135 позиций разобрано (известное ограничение — см. Verdict A по №354)",
          len(parsed["records"]) == 135, f"records={len(parsed['records'])}")
    check("ни одно нарушение не закрыто молча (removed пуст)",
          result["removed"] == [], f"removed={[r['sys_no'] for r in result['removed']]}")
    check("все 17 (16 устранённых + №354) — на проверку, ни одного не пропущено",
          review_sysnos == sorted([100, 127, 151, 163, 164, 176, 218, 224, 255, 327,
                                    331, 354, 367, 376, 380, 384, 385]),
          f"review_sysnos={review_sysnos}")
    entry_354 = next((c for c in result["closure_review"] if c["violation"]["sys_no"] == 354), None)
    check("№354 на проверке с resolved=false (не подтверждено устранение — не парсер решает)",
          entry_354 is not None and entry_354["violation"]["resolved"] is False)


def test_shifted_layout():
    print("\n=== (в) Сдвинутый формат таблицы (синтетические координаты, см. докстринг) ===")
    # "Нормальный" акт: цифры 1/2/3/4 сигнатуры лежат ровно в границах
    # BOUNDS. Здесь all колонки сдвинуты на +80pt вправо — имитация
    # документа с другой вёрсткой таблицы (другой шаблон/масштаб печати).
    shift = 80.0
    fake_words = [
        {"text": "1", "top": 231.4, "x0": 70.8 + shift},
        {"text": "2", "top": 231.4, "x0": 204.9 + shift},
        {"text": "3", "top": 231.4, "x0": 386.2 + shift},
        {"text": "4", "top": 231.4, "x0": 500.4 + shift},
    ]

    class FakePage:
        def extract_words(self):
            return fake_words

    class FakePdf:
        pages = [FakePage(), FakePage(), FakePage()]

    check("сдвинутая сигнатура НЕ проходит verify_column_header()",
          rp.verify_column_header(FakePdf()) is False)

    # Контрольная проверка — тот же вход БЕЗ сдвига обязан пройти,
    # иначе тест (в) доказывал бы не то (что угодно возвращает False).
    for w in fake_words:
        w["x0"] -= shift

    class FakePdfOk:
        pages = [FakePage(), FakePage(), FakePage()]

    check("та же сигнатура БЕЗ сдвига проходит verify_column_header() (контроль)",
          rp.verify_column_header(FakePdfOk()) is True)


def test_violation_354_regression():
    """Координатор, 28.09.2026 — реальная пара текстов, на которой
    структурное закрытие ушло молча (акт 4183-162, нарушение №354):
    старое нарушение о СИЗ/касках не совпало ни с чем в новом акте по
    старому порогу (RSK_MATCH_CANDIDATE=0.55) и закрылось без вопроса,
    хотя реально не снято (координатор подтвердил). Лучший конкурент
    («4.3», про дорожные знаки при въезде — совсем другая тема) набирал
    0.407 — ниже 0.55, но ВЫШЕ RSK_CLOSURE_REVIEW_THRESHOLD (0.40),
    введённого этой же правкой. Регрессионный тест фиксирует ровно эту
    пару, чтобы порог/логика не разъехались молча в будущем."""
    print("\n=== Регрессия №354 (реальные тексты акта 4183-159 → 4183-162) ===")
    sys.path.insert(0, "/app")
    import main as m

    old_text = ("У персонала, занятого на производстве работ, отсутствуют средства "
                "индивидуальной защиты (каски). Нарушены требования: п. 30, 31 «Приказ "
                "№ 883н \"Об утверждении правил по охране труда при строительстве\" от "
                "11.12.2020»; п. 5.13 СНиП 12-03-2001 «Безопасность труда в строительстве. "
                "Часть 1. Общие требования»; ч. 6 ст. 52 ГрК РФ.")
    new_text_43 = ("При въезде на строительную площадку не установлены дорожные знаки. "
                   "Нарушены требования проектной документации л. 16 шифр "
                   "2020.069.3000-ТМ-ПОС-ПЗ; ч. 6 ст. 52 ГрК РФ.")

    norm_old = m.norm_literal(old_text)
    norm_new = m.norm_literal(new_text_43)
    score = m._rsk_text_similarity(norm_old, norm_new)
    print(f"  score(354, «4.3») = {score:.3f}")
    check("счёт похожести зафиксирован в известном диапазоне 0.35-0.45 (документирует находку)",
          0.35 <= score <= 0.45, f"score={score:.3f}")
    check("счёт НИЖЕ старого RSK_MATCH_CANDIDATE (0.55) — по старой логике не было бы даже кандидатом",
          score < m.RSK_MATCH_CANDIDATE, f"score={score:.3f} vs {m.RSK_MATCH_CANDIDATE}")
    check("счёт ВЫШЕ нового RSK_CLOSURE_REVIEW_THRESHOLD (0.40) — новая защита обязана сработать",
          score >= m.RSK_CLOSURE_REVIEW_THRESHOLD, f"score={score:.3f} vs {m.RSK_CLOSURE_REVIEW_THRESHOLD}")

    # Прямая проверка защитной функции на этой самой паре (не через БД —
    # синтетический "старый" кандидат и синтетическая "новая" запись).
    fake_old = {"violation_id": -1, "sys_no": 354, "content": old_text,
                "_norm": norm_old, "control_section": 4, "resolved": False}
    fake_new_records = [{"item_no": "4.3", "control_section": 4, "content": new_text_43, "sys_no": None}]
    candidates = m._rsk_closure_review_candidates(fake_old, fake_new_records, plan={})
    check("_rsk_closure_review_candidates находит эту пару (не пустой список)",
          len(candidates) == 1 and candidates[0]["item_no"] == "4.3",
          f"candidates={candidates}")


if __name__ == "__main__":
    import os

    real_pdf_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test_act.pdf"

    if os.path.exists(real_pdf_path):
        test_image_only(real_pdf_path)
        test_real_act(real_pdf_path)
    else:
        print(f"\n(а)/(б) ПРОПУЩЕНЫ — файл {real_pdf_path} не найден на диске "
              f"(PDF акта не хранится в репозитории между сессиями)")

    test_shifted_layout()
    test_violation_354_regression()
    test_image_only_page_detection()
    test_declared_vs_parsed_guard()
    test_boilerplate_stripping()
    test_replay_act_4183_162_closure_policy()

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} — {FAILURES}")
        sys.exit(1)
    print("Все проверки прошли.")
