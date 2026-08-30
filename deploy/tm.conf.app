server {
    listen 80;
    server_name tm.asd-kontur.ru;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name tm.asd-kontur.ru;

    ssl_certificate /etc/letsencrypt/live/tm.asd-kontur.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tm.asd-kontur.ru/privkey.pem;

    add_header X-Robots-Tag "noindex, nofollow" always;

    client_max_body_size 5m;

    location / {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/tm.htpasswd;

        proxy_pass http://tm_backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
