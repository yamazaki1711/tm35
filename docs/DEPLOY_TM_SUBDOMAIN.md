# Публикация tm.asd-kontur.ru — статус

**Итог: выполнено 14–15.08.2026.** Поддомен открывается по HTTPS,
сертификат валиден, без Basic Auth — 401, backend/дашборд НЕ
опубликован — только служебная заглушка.

## DNS

A-запись `tm.asd-kontur.ru → 147.45.251.207` создана координатором в
Timeweb. Резолвинг подтверждён публичными резолверами (8.8.8.8, 1.1.1.1,
9.9.9.9) — первые проверки после создания записи «мигали» между NXDOMAIN
и корректным ответом (нормально для anycast-сетей в первые минуты после
создания записи, TTL 600), стабилизировался за ~15 минут.

## Инфраструктура (по образцу bi.asd-kontur.ru)

- Сервер: kat-core, `root@147.45.251.207`, Docker Compose проект `/opt/kat`.
- Nginx (`kat_nginx`, образ `nginx:alpine`) обслуживает все домены одним
  контейнером; конфиги — отдельные файлы в `/opt/kat/nginx/*.conf`,
  смонтированные bind-mount'ами в `docker-compose.yml`.
- Новый файл: `/opt/kat/nginx/tm.conf` — два server-блока (80 → редирект
  + acme-challenge; 443 → статика + Basic Auth), по образцу `bi.conf`.
- Статика: `/opt/kat/tm/html/index.html` — заглушка (одна страница, без
  фреймворков), смонтирована в `/var/www/tm` (read-only).
- Basic Auth: `/opt/kat/tm/.htpasswd`, bcrypt cost 10 (тот же формат, что
  у `bi/.htpasswd`), **владелец файла `www-data:www-data`, права 644** —
  это обязательно: worker-процесс nginx внутри контейнера работает под
  uid `nginx` (101), не root, и не сможет прочитать файл с owner `root`
  без «world-readable». Учётные данные — логин/пароль от координатора
  (`tm-35` / см. `.secrets/tm_basic_auth.env`), не сгенерированы мной —
  единый пользователь для будущего фронтенда и формы, как было явно
  указано.
- `docker-compose.yml` на сервере: добавлены 3 строки монтирования под
  `nginx.volumes` (tm.conf, tm/html, tm/.htpasswd), рядом со строками для
  bi. Бэкап перед правкой — `docker-compose.yml.bak.20260814_192457`
  (на сервере, не переписан).

## Сертификат

```
certbot certonly --webroot -w /opt/kat/certbot-www -d tm.asd-kontur.ru \
  --key-type ecdsa --non-interactive --agree-tos -n
```

Тем же способом (webroot, ECDSA), что и остальные сертификаты на этом
сервере — тот же certbot-аккаунт, тот же `certbot.timer` подхватит
автопродление (никакой отдельной настройки cron не потребовалось).

```
Successfully received certificate.
Certificate is saved at: /etc/letsencrypt/live/tm.asd-kontur.ru/fullchain.pem
Key is saved at:         /etc/letsencrypt/live/tm.asd-kontur.ru/privkey.pem
This certificate expires on 2026-11-12.
```

Проверено `openssl s_client -servername tm.asd-kontur.ru` — сервер
отдаёт именно этот сертификат (не сертификат bi/asd-kontur по SNI-
умолчанию, см. ниже про баг).

## Два дефекта, найденные и исправленные в процессе (не догадки — оба
## воспроизведены, диагностированы по логам, устранены)

1. **Bind-mount отдельного файла привязан к inode, не к пути.** После
   `mv` нового `tm.conf` на место старого контейнер продолжал видеть
   старое содержимое (только порт 80, без блока 443) — `nginx -t` и
   `nginx -s reload` молча «проходили», потому что валидировали
   старый файл. Обнаружено через `docker exec kat_nginx cat
   /etc/nginx/conf.d/tm.conf` — не совпадало с файлом на хосте.
   Исправлено пересозданием контейнера: `docker compose up -d --no-deps
   --force-recreate nginx` (обычный `up -d` без `--force-recreate` не
   пересоздаёт контейнер, если сам `docker-compose.yml` не менялся —
   тоже пришлось выяснить опытным путём).
2. **Права на `.htpasswd`.** Файл, созданный от `root`, лежал `640
   root:root` — worker nginx (uid 101, не root) не мог его прочитать →
   `500 Internal Server Error` с `open() ... Permission denied` в логе
   контейнера при попытке провалидировать Basic Auth (без заголовка
   `Authorization` до этого момента отдавался корректный 401, поэтому
   баг проявился только на втором тесте, с реальными кредами). Найдено
   через `docker logs kat_nginx` (не `tail -f /var/log/nginx/error.log`
   внутри контейнера — этот путь симлинк на `/dev/stderr` и виснет без
   EOF, тоже выяснено опытным путём и не повторялось второй раз).
   Исправлено: `chmod 644` + `chown www-data:www-data`, по образцу
   действующего `bi/.htpasswd`.

## Финальная проверка

```
asd-kontur.ru:                200   (не задет)
bi.asd-kontur.ru (без пароля): 401   (не задет)
tm.asd-kontur.ru (без пароля): 401
tm.asd-kontur.ru (неверный пароль): 401
tm.asd-kontur.ru (верный пароль): 200, отдаёт заглушку "ТМ-35 Мониторинг"
SNI-сертификат для tm.asd-kontur.ru: CN=tm.asd-kontur.ru, действителен до 2026-11-12
```

## Обновление 15.08.2026, ночной прогон — заглушка заменена на рабочий backend

`tm.conf` теперь проксирует `location /` на контейнер `tm_backend:8000`
(FastAPI, тот же паттерн, что `bi.conf` → `id-track:8000`) вместо раздачи
статики — basic-auth (`auth_basic`) остался на том же location, тот же
`/etc/nginx/tm.htpasswd`. Новый сервис `tm_backend` добавлен в
`docker-compose.yml` (build `./tm-app`, `env_file: ./tm-app/.env` —
пароль `tm35_app` НЕ в самом compose-файле, по образцу `postgres`/
`backend`), на сети `kat_default`, без публикации порта наружу (`expose:
8000`), достаётся только через nginx. Подробности реализации backend,
дашбордов, формы и найденных по ходу багов — `docs/NIGHT_RUN_LOG.md`.

Пароль `tm35_app` был ротирован в процессе этого шага (засветился в
выводе SSH-команды дважды) — старый пароль недействителен, актуальный в
`.secrets/tm35_app.env` и на сервере в `/opt/kat/tm-app/.env`.
