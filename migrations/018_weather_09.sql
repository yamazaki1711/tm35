-- Координатор, 29.08.2026: погода в /report должна быть на 09:00 утра по
-- времени объекта (начало смены), не суточный агрегат. Суточные поля НЕ
-- удаляются (правило проекта — уже наполнены за 01.07-28.08, могут
-- понадобиться) — только добавляются новые под замер на 09:00.

alter table daily_weather
    add column temp_09_c numeric,
    add column precipitation_09_mm numeric,
    add column wind_09_ms numeric,
    add column weathercode_09 integer;
