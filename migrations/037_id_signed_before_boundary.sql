-- ТЗ Якименко А.И. (ПТО), 16.09.2026 — тайл «Подписано ранее, ₽»: папки,
-- подписанные ДО 10.03.2026 (до того, как Якименко принял участок), уже
-- закрыты и не входят в остаток по контракту. Граница хранится в
-- app_setting, не буквальным числом в коде — координатор может
-- поправить дату без деплоя (прямой UPDATE app_setting, см. get_app_setting()).
begin;

insert into app_setting (key, value, updated_at)
values ('id_signed_before_boundary_date', '2026-03-10', now())
on conflict (key) do nothing;

commit;
