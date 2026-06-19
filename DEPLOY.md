# Деплой Agent 3 PM Tracker на VPS

## Требования
- Ubuntu 20.04+ VPS с root-доступом
- Минимум 1 GB RAM, 10 GB диск
- Открытые порты: 22 (SSH), 80, 443, 8080

## Шаг 1: Подключись к серверу

```bash
ssh root@YOUR_SERVER_IP
```

## Шаг 2: Установи Docker

```bash
apt update && apt install -y docker.io docker-compose git
systemctl enable docker
systemctl start docker
```

## Шаг 3: Клонируй репозиторий

```bash
cd /opt
git clone https://github.com/AmirITMO/Agent-for-PM.git
cd Agent-for-PM
```

Если репозиторий приватный — сначала запушь с локальной машины:
```bash
# На ЛОКАЛЬНОЙ машине (Windows):
cd c:\Agent-for-PM
git add -A
git commit -m "Deploy ready"
git remote add origin https://github.com/AmirITMO/Agent-for-PM.git
git push -u origin main
```

## Шаг 4: Создай .env файл на сервере

```bash
cat > /opt/Agent-for-PM/.env << 'EOF'
TELEGRAM_BOT_TOKEN=8852273053:AAFFhrXmXk2CyDgNneMaw0fyL8VEv775pFY
OPENAI_API_KEY=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
DATABASE_URL=postgresql+asyncpg://postgres:agent3secure2026@agent3-db:5432/agent3_pm
DATABASE_URL_SYNC=postgresql://postgres:agent3secure2026@agent3-db:5432/agent3_pm
WEB_BASE_URL=http://YOUR_SERVER_IP:8083
WEB_HOST=0.0.0.0
WEB_PORT=8080
TIMEZONE=Europe/Moscow
MORNING_SUMMARY_HOUR=9
MORNING_SUMMARY_MINUTE=0
DEADLINE_CHECK_INTERVAL_MINUTES=30
DEADLINE_WARNING_HOURS=24
SECRET_KEY=ПОМЕНЯЙ_НА_СЛУЧАЙНУЮ_СТРОКУ
EOF
```

**ВАЖНО:** замени `YOUR_SERVER_IP` на реальный IP сервера.

## Шаг 5: Обнови docker-compose.yml

```bash
cat > /opt/Agent-for-PM/docker-compose.yml << 'EOF'
version: '3.8'

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: agent3_pm
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    volumes:
      - pgdata:/var/lib/postgresql/data
    restart: unless-stopped

  agent3:
    build: .
    depends_on:
      - db
    env_file: .env
    ports:
      - "8080:8080"
    volumes:
      - uploads:/app/agent3_pm/uploads
    restart: unless-stopped

volumes:
  pgdata:
  uploads:
EOF
```

## Шаг 6: Обнови Dockerfile

```bash
cat > /opt/Agent-for-PM/Dockerfile << 'EOF'
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt aiohttp

COPY . .

CMD ["python", "-m", "agent3_pm.main"]
EOF
```

## Шаг 7: Запусти

```bash
cd /opt/Agent-for-PM
docker-compose up -d --build
```

Подожди 30 секунд, проверь:

```bash
# Проверить что контейнеры запущены
docker-compose ps

# Логи приложения
docker-compose logs -f agent3

# Заполнить тестовыми данными (первый раз)
docker-compose exec agent3 python -m agent3_pm.seed
```

## Шаг 8: Проверь

Открой в браузере: `http://YOUR_SERVER_IP:8080`

Бот в Telegram должен отвечать.

## Полезные команды

```bash
# Перезапустить
docker-compose restart agent3

# Обновить код
cd /opt/Agent-for-PM
git pull
docker-compose up -d --build

# Посмотреть логи
docker-compose logs -f agent3

# Зайти в БД
docker-compose exec db psql -U postgres -d agent3_pm

# Бэкап БД
docker-compose exec db pg_dump -U postgres agent3_pm > backup.sql

# Восстановить бэкап
cat backup.sql | docker-compose exec -T db psql -U postgres -d agent3_pm
```

## Если нужен домен + HTTPS (для кнопок в Telegram)

Без домена Telegram не позволяет кнопки-ссылки в боте (только текстовые URL).
Если появится домен:

```bash
# Установи Caddy (автоматический HTTPS)
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# Настрой реверс-прокси
echo 'your-domain.ru {
    reverse_proxy localhost:8080
}' > /etc/caddy/Caddyfile
systemctl restart caddy

# Обнови .env
# WEB_BASE_URL=https://your-domain.ru
docker-compose restart agent3
```
