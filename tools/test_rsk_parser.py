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

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} — {FAILURES}")
        sys.exit(1)
    print("Все проверки прошли.")
