#!/usr/bin/env python3
"""
Заход 4, 10.09.2026, задача 3 — тест идемпотентности пересчёта
category->tabs (`main.rerun_category_mapping`).

Вызывает РЕАЛЬНУЮ функцию, которую дёргает кнопка "Сохранить" на
/id-grafik/category-mapping (main.py:rerun_category_mapping), не её
копию — иначе тест проверял бы не тот код, что реально исполняется.

Пишет в БД по-настоящему (иначе идемпотентность "второй запуск ничего
не меняет" нечем было бы подтвердить), но полностью убирает за собой:
запоминает состояние `id_report_group_row` для затронутой категории до
теста и восстанавливает его в конце, даже при ошибке (try/finally).
Работает на первой попавшейся категории с пустыми группами и хотя бы
одним ненулевым совпадением в матрице доказательств — при пустой БД
(все категории уже сопоставлены) сообщает об этом и завершается
успешно, не бросая ложный отказ.

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/test_category_mapping_rerun.py
"""
import sys

sys.path.insert(0, "/app")
import main as m  # noqa: E402

FAILED = False


def check(name, condition, detail=""):
    global FAILED
    mark = "PASS" if condition else "FAIL"
    if not condition:
        FAILED = True
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def main():
    matrix_data = m.compute_category_tab_evidence_matrix()
    target = None
    target_tab_id = None
    for row in matrix_data["matrix"]:
        for tab in matrix_data["tabs"]:
            if row["counts"][tab["id"]] > 0:
                target = row["category"]
                target_tab_id = tab["id"]
                break
        if target:
            break

    if not target:
        print("Нет ни одной категории с ненулевым совпадением в матрице доказательств — "
              "тест идемпотентности нечем провести содержательно, пропущен.")
        sys.exit(0)

    print(f"Категория для теста: «{target}», вкладка id={target_tab_id}")

    group_ids = [g["id"] for g in m.query(
        "select id from id_report_group where category_group=%s", (target,)
    )]
    before_rows = {
        (r["group_id"], r["row_id"])
        for r in m.query("select group_id, row_id from id_report_group_row where group_id = any(%s)", (group_ids,))
    }
    before_count_total = m.query_one("select count(*) as n from id_report_group_row")["n"]

    try:
        result1 = m.rerun_category_mapping(target, {target_tab_id})
        after_first_count = m.query_one("select count(*) as n from id_report_group_row")["n"]
        after_first_rows = {
            (r["group_id"], r["row_id"])
            for r in m.query("select group_id, row_id from id_report_group_row where group_id = any(%s)", (group_ids,))
        }

        check(
            "первый запуск действительно что-то изменил (иначе тест ничего не проверяет)",
            after_first_count > before_count_total,
            f"{before_count_total} -> {after_first_count}",
        )

        result2 = m.rerun_category_mapping(target, {target_tab_id})
        after_second_count = m.query_one("select count(*) as n from id_report_group_row")["n"]
        after_second_rows = {
            (r["group_id"], r["row_id"])
            for r in m.query("select group_id, row_id from id_report_group_row where group_id = any(%s)", (group_ids,))
        }

        check(
            "второй запуск не добавил новых строк (идемпотентность по количеству)",
            after_first_count == after_second_count,
            f"{after_first_count} -> {after_second_count}",
        )
        check(
            "второй запуск не изменил состав связей (идемпотентность по содержимому)",
            after_first_rows == after_second_rows,
            f"разница: {after_first_rows ^ after_second_rows}",
        )
        check(
            "второй запуск не сообщает о новых 'получивших раздел' группах",
            len(result2["gained"]) == 0,
            f"result2['gained']={result2['gained']}",
        )

        result3 = m.rerun_category_mapping(target, {target_tab_id})
        after_third_count = m.query_one("select count(*) as n from id_report_group_row")["n"]
        check(
            "третий запуск подряд — тоже без изменений",
            after_third_count == after_second_count,
            f"{after_second_count} -> {after_third_count}",
        )
    finally:
        # Откат — тестовые изменения не остаются в БД, ровно то, что
        # требует "no default mapping is pre-filled" для остальных
        # категорий, применённое и к этой (эта — тоже не решение
        # координатора, а тестовый прогон).
        def _restore(cur):
            cur.execute("delete from id_report_group_row where group_id = any(%s)", (group_ids,))
            for group_id, row_id in before_rows:
                cur.execute(
                    "insert into id_report_group_row (group_id, row_id) values (%s, %s)",
                    (group_id, row_id),
                )
        m.run_in_transaction(_restore)
        restored_count = m.query_one("select count(*) as n from id_report_group_row")["n"]
        check(
            "откат теста восстановил исходное общее число связей раздел-группа",
            restored_count == before_count_total,
            f"{before_count_total} -> {restored_count}",
        )

    print()
    print("ОТКАЗ" if FAILED else "Все проверки идемпотентности пересчёта category-mapping прошли.")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
