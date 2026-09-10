#!/usr/bin/env python3
"""
Заход 3, 10.09.2026, задача 5 — тест diff-логики закрытия нарушений РСК.

`_rsk_diff_vs_previous()` (main.py) уже реализована и уже вызывается на
каждой загрузке акта (`/rsk/import`, предпросмотр) и на подтверждении
(`/rsk/import/confirm`, где сравнение sys_no реально закрывает
нарушения через closed_in_act_id) — это не "подготовка на будущее", это
рабочий код, который сейчас закрывает 0 нарушений просто потому, что в
БД пока один акт. Задача 5 — тест этой логики: диффить с реальным
единственным актом (только читаем, не пишем ничего), синтетический
"акт N+1" строим в памяти вычитанием заранее известного подмножества
sys_no из реальных позиций акта — НЕ загружаем в БД (запрещено заданием
явно), после теста ничего в БД не остаётся, что не было там до запуска.

Закрытие проверяется структурно: пересечение множеств sys_no, без
разбора содержания замечания — так же, как устроен сам продакшн-код
(main.py:6947, "снятие — структурно, никогда по человеческому тексту").

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/test_rsk_act_diff.py
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
    acts_before = m.query_one("select count(*) as n from rsk_act")["n"]
    violations_before = m.query_one("select count(*) as n from rsk_violation")["n"]

    prev_act = m.query_one("select id, act_no from rsk_act order by act_date desc, id desc limit 1")
    if not prev_act:
        print("Нет ни одного акта РСК в БД — тест diff-логики нечем проверить, пропущен.")
        sys.exit(0)

    real_items = m.query(
        "select v.sys_no, i.content, i.remedy from rsk_act_item i "
        "join rsk_violation v on v.id = i.violation_id where i.act_id = %s order by v.sys_no",
        (prev_act["id"],),
    )
    check(f"в акте {prev_act['act_no']} есть позиции для теста", len(real_items) >= 3,
          f"{len(real_items)} позиций")
    if len(real_items) < 3:
        sys.exit(1)

    # Синтетический акт N+1 = реальный акт МИНУС известное подмножество
    # sys_no (первые 2 и последний) — не пишется в БД нигде в этом файле.
    dropped_sysnos = sorted({real_items[0]["sys_no"], real_items[1]["sys_no"], real_items[-1]["sys_no"]})
    kept_records = [dict(r) for r in real_items if r["sys_no"] not in dropped_sysnos]

    check("синтетический акт действительно меньше реального ровно на заявленное подмножество",
          len(kept_records) == len(real_items) - len(dropped_sysnos),
          f"{len(real_items)} -> {len(kept_records)}, ожидалось убрать {len(dropped_sysnos)}")

    diff = m._rsk_diff_vs_previous(kept_records)

    check("diff['removed'] содержит ровно заявленное подмножество, не больше и не меньше",
          diff["removed"] == dropped_sysnos,
          f"ожидалось {dropped_sysnos}, получено {diff['removed']}")

    check("diff['new'] пуст — синтетический акт не добавлял новых sys_no",
          diff["new"] == [],
          f"получено {diff['new']}")

    check("diff['changed'] пуст — содержание оставшихся позиций не менялось",
          diff["changed"] == [],
          f"получено {diff['changed']}")

    check("diff['unchanged'] покрывает все оставшиеся позиции",
          sorted(diff["unchanged"]) == sorted(r["sys_no"] for r in kept_records),
          f"{len(diff['unchanged'])} vs {len(kept_records)}")

    # Изменение содержания одной оставшейся позиции — должно попасть в
    # changed, не в unchanged и не в removed (структурная проверка тоже
    # ловит текстовые правки, просто не решает по ним закрытие).
    altered = [dict(r) for r in kept_records]
    altered[0]["content"] = (altered[0]["content"] or "") + " ДОПОЛНЕНО ДЛЯ ТЕСТА"
    diff2 = m._rsk_diff_vs_previous(altered)
    check("правка содержания одной позиции отражается в diff['changed'], не в removed",
          altered[0]["sys_no"] in diff2["changed"] and altered[0]["sys_no"] not in diff2["removed"],
          f"changed={diff2['changed'][:5]}, removed={diff2['removed']}")

    # Ничего не записывалось — прямая проверка числами до/после, не
    # только "мы не вызывали run_in_transaction" на словах.
    acts_after = m.query_one("select count(*) as n from rsk_act")["n"]
    violations_after = m.query_one("select count(*) as n from rsk_violation")["n"]
    check("БД не тронута тестом: число актов и нарушений не изменилось",
          acts_after == acts_before and violations_after == violations_before,
          f"акты {acts_before}->{acts_after}, нарушения {violations_before}->{violations_after}")

    n_fail = 0 if not FAILED else 1
    print()
    print("ОТКАЗ" if FAILED else "Все проверки diff-логики закрытия РСК прошли.")
    sys.exit(n_fail)


if __name__ == "__main__":
    main()
