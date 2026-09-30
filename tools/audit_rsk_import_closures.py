#!/usr/bin/env python3
"""
Координатор, 28.09.2026 (нарушение №354, docs/RUN_20260928_rsk_-
violation_354_fix.md) — после КАЖДОГО импорта акта РСК стоит перепроверить
закрытия, которые он произвёл: для каждого закрытого нарушения — лучший
кандидат среди ВСЕХ позиций акта, который его закрыл (без ограничения
разделом, по обеим метрикам похожести — `_rsk_text_similarity()`, та же
функция, что и в матчере), плюс явный признак «отмечено ли «Устранено»
до этого импорта» — самый сильный практический сигнал: закрытие
нарушения, которое НИКТО не считал устранённым, заслуживает взгляда
живого человека, даже если новый порог `RSK_CLOSURE_REVIEW_THRESHOLD`
почему-то не сработал (например, если акт загружен без предпросмотра —
такого пути в UI нет, но скрипт полезен и как независимая, отдельная от
UI сверка).

Только чтение, ничего не пишет и не исправляет — список для доклада,
автоматических правок нет ни для одной строки (координатор явно этого
не хочет, каждое закрытие № решается отдельно).

Запуск — внутри контейнера tm_backend:
    docker exec tm_backend python3 tools/audit_rsk_import_closures.py <act_id>

Без аргумента — последний загруженный акт (`rsk_act`, order by
imported_ts desc).
"""
import sys

sys.path.insert(0, "/app")
import main as m  # noqa: E402


def audit_closures(act_id):
    act = m.query_one("select id, act_no, act_date from rsk_act where id=%s", (act_id,))
    if not act:
        print(f"Акт id={act_id} не найден.")
        return 1
    print(f"Акт {act['act_no']} от {act['act_date']} (id={act_id})")

    closed = m.query("""
        select v.id, v.sys_no, coalesce(p.resolved,false) as resolved
        from rsk_violation v left join rsk_processing p on p.violation_id=v.id
        where v.closed_in_act_id = %s order by v.sys_no
    """, (act_id,))
    if not closed:
        print("Этот акт ничего не закрыл.")
        return 0

    new_items = m.query("select item_no, control_section, content from rsk_act_item where act_id=%s", (act_id,))
    for it in new_items:
        it["_norm"] = m.norm_literal(it["content"] or "")

    print(f"Закрыто нарушений: {len(closed)}\n")
    print(f"{'sys_no':>7} {'Устранено':>10} {'счёт':>7}  позиция  содержание (обрезано)")
    flagged = []
    for v in closed:
        old_item = m.query_one(
            "select content from rsk_act_item where violation_id=%s and act_id < %s order by act_id desc limit 1",
            (v["id"], act_id),
        )
        old_norm = m.norm_literal(old_item["content"] or "") if old_item else ""
        best = None
        for it in new_items:
            score = m._rsk_text_similarity(old_norm, it["_norm"])
            if best is None or score > best[0]:
                best = (score, it)
        score, it = best if best else (0.0, {"item_no": "—", "content": ""})
        flag = "  <-- ПОСМОТРЕТЬ" if (not v["resolved"] or score >= m.RSK_CLOSURE_REVIEW_THRESHOLD) else ""
        print(f"{v['sys_no']:>7} {str(v['resolved']):>10} {score:>7.3f}  {it['item_no']:>7}  {(it['content'] or '')[:60]}{flag}")
        if not v["resolved"] or score >= m.RSK_CLOSURE_REVIEW_THRESHOLD:
            flagged.append((v, score, it, old_item["content"] if old_item else None))

    print(f"\nВсего закрыто: {len(closed)}, требуют взгляда: {len(flagged)}")
    for v, score, it, old_content in flagged:
        print(f"\n=== №{v['sys_no']} (Устранено={v['resolved']}, счёт={score:.3f}) ===")
        print("  БЫЛО:", old_content)
        print(f"  ПОХОЖЕ (п.{it['item_no']}):", it["content"])
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_act_id = int(sys.argv[1])
    else:
        latest = m.query_one("select id from rsk_act order by imported_ts desc limit 1")
        target_act_id = latest["id"] if latest else None
    if target_act_id is None:
        print("Нет ни одного загруженного акта.")
        sys.exit(1)
    sys.exit(audit_closures(target_act_id))
