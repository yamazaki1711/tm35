# Импорт контура РСК

См. `docs/RSK_KONTUR_IMPORT_2026-09-06.md` — что импортировано, как
обработаны дефекты источника, что осталось на ручную доводку.

Запуск (повторный безопасен — upsert по `sys_no`):

```
python3 parse_rsk.py          # xlsx + pdf -> parsed.json, ничего не пишет в БД
TM35_DSN=... python3 load_rsk.py   # parsed.json -> Postgres
```

Источники — `../../Реестр нарушений ТМ-35 13.07.2026.xlsx` (лист
`Violations`) и `../../Акт проверки № 4183-159 от 11.08.2026 (1).pdf`,
пути захардкожены в `parse_rsk.py` (`XLSX_PATH`/`PDF_PATH`).
