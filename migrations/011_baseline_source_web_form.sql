-- Форма плановых сроков (/baseline, 28.08.2026) — до сих пор
-- baseline_source знал только источники исходного Excel-импорта
-- (matrix_schedule/text_month_only/no_data). Запись, введённая
-- человеком через веб-форму, не подходит ни под один из них —
-- маскировать её под 'matrix_schedule' значило бы соврать про
-- происхождение даты. Добавлено значение 'web_form', тем же именем,
-- что уже используют daily_progress.source и id_package (везде в
-- проекте web_form/excel_import — устоявшаяся пара, не новая идея).

alter type baseline_source add value 'web_form';
