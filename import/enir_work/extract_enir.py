"""
Шаг 2: извлечь параграфы норм (§) из сборников ЕНиР (уже сконвертированных
libreoffice в HTML -> очищенный текст).

Формат § (проверено на Е5 Вып.1 и Е22 Вып.1, оба варианта совпадают):
  строка, состоящая ровно из "§" (заголовочный маркер — inline-упоминания
  внутри других абзацев на отдельной строке не остаются, только реальные
  заголовки параграфов);
  следующая строка(и) до первого ". " — код (Е5-1-1, Е22-1-9а и т.п.) и
  название работы;
  далее блок текста до следующего такого маркера — "Состав работы"/
  "Состав звена"/"Указания по применению норм"/"Нормы времени и расценки
  на <единица>" и таблица.

Извлекаем НАДЁЖНО: код, название, состав звена/работы (сырой текст),
единицу измерения (из фразы "на <единица>"). Численные нормы времени
(Н.вр.) из таблицы не парсим как одно число — таблицы параметрические
(зависят от способа производства, диаметра, массы и т.д.), доставать
одно "правильное" число автоматически — риск тихой ошибки. Вместо этого
сохраняем сырой текст таблицы целиком, чтобы человек мог посмотреть
исходник по указанному §.
"""
import json
import re
import sys
from pathlib import Path

CODE_RE = re.compile(r"^\s*(Е\s?\d+[а-я]?-\d+(?:-\d+)?[а-я]?)\.\s*(.*)$", re.UNICODE)
UNIT_STOP = r"(?=\.|\s(?:Способ|Состав|Указания|Технические|Организация|Таблица|Примечание)\b|$)"
UNIT_RE = re.compile(r"[Нн]орм[ыа]\s+времени[^.]*?\bна\s+(.+?)" + UNIT_STOP, re.UNICODE)


def html_to_text(html_path: Path) -> str:
    html = html_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", "\n", html)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def split_blocks(text: str):
    lines = text.split("\n")
    marker_idx = [i for i, ln in enumerate(lines) if ln == "§"]
    blocks = []
    for k, idx in enumerate(marker_idx):
        end = marker_idx[k + 1] if k + 1 < len(marker_idx) else len(lines)
        blocks.append(lines[idx + 1:end])
    return blocks


def parse_block(block_lines, sbornik_name):
    if not block_lines:
        return None
    # Код+название могут занимать несколько строк подряд, пока не встретится
    # известный служебный заголовок раздела (Состав/Указания/Нормы/Технические...).
    STOP_HEADERS = {"Состав", "Указания", "Нормы", "Технические", "Организация",
                     "Примечание", "Примечания", "Таблица"}
    header_lines = []
    i = 0
    while i < len(block_lines) and block_lines[i] not in STOP_HEADERS:
        header_lines.append(block_lines[i])
        i += 1
    header_text = " ".join(header_lines).strip()
    m = CODE_RE.match(header_text)
    if not m:
        return None
    code, title = m.group(1).replace(" ", ""), m.group(2).strip()

    rest_text = " ".join(block_lines[i:])
    unit_m = UNIT_RE.search(rest_text)
    unit_phrase = unit_m.group(1).strip() if unit_m else None

    # Состав звена — только настоящие пары "Состав"+"звена" (заголовок
    # состава исполнителей, часто повторяется как шапка колонки таблицы
    # для разных способов производства работ). "Состав работы"/"Состав
    # работ" — это описание технологии, не звена, не используем как
    # источник специализации/разряда, чтобы не выдавать одно за другое.
    # Берём ВСЕ вхождения (в одном § бывает несколько вариантов звена —
    # краном/вручную и т.п.), склеиваем через " | ".
    crew_fragments = []
    j = i
    while j < len(block_lines) - 1:
        if block_lines[j] == "Состав" and block_lines[j + 1] == "звена":
            k = j + 2
            frag_lines = []
            while k < len(block_lines) and block_lines[k] not in STOP_HEADERS:
                frag_lines.append(block_lines[k])
                k += 1
            frag = " ".join(frag_lines).strip()
            if frag:
                crew_fragments.append(frag)
            j = k
        else:
            j += 1
    crew_text = " | ".join(crew_fragments) if crew_fragments else None

    return {
        "sbornik": sbornik_name,
        "code": code,
        "title": title,
        "unit_phrase": unit_phrase,
        "crew_raw": crew_text,
        "raw_block_chars": len(rest_text),
    }


def main():
    conv_dir = Path("/home/oleg/Documents/TM-35/import/enir_work/conv")
    out_path = Path("/home/oleg/Documents/TM-35/import/enir_work/enir_norms.json")

    all_norms = []
    stats = []
    for html_path in sorted(conv_dir.glob("*.html")):
        sbornik_name = html_path.stem
        text = html_to_text(html_path)
        blocks = split_blocks(text)
        parsed = [parse_block(b, sbornik_name) for b in blocks]
        parsed = [p for p in parsed if p]
        all_norms.extend(parsed)
        stats.append((sbornik_name, len(blocks), len(parsed)))

    out_path.write_text(json.dumps(all_norms, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Всего параграфов извлечено: {len(all_norms)}")
    print(f"{'Сборник':50s} {'§-маркеров':>10s} {'разобрано':>10s}")
    for name, markers, parsed_n in stats:
        print(f"{name:50s} {markers:>10d} {parsed_n:>10d}")


if __name__ == "__main__":
    main()
