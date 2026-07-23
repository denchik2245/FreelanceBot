# Развёртывание бота на VPS REG.RU

## Какой сервер выбрать

Для текущего бота достаточно:

- образ: Docker на Ubuntu 24.04 LTS;
- 1 vCPU;
- 1 ГБ RAM;
- 10 ГБ SSD;
- публичный IPv4;
- вход по SSH-ключу.

Домен, Nginx, ISPmanager и открытые веб-порты боту не нужны: он только сам обращается
к VK и биржам по HTTPS.

## Создание сервера

1. В личном кабинете REG.RU откройте «Облако».
2. Выберите «Новый ресурс» → «Сервер».
3. Выберите приложение Docker на Ubuntu 24.04 LTS.
4. Выберите минимальную конфигурацию 1 vCPU / 1 ГБ RAM / 10 ГБ SSD.
5. Добавьте SSH-ключ и создайте сервер.
6. Сохраните публичный IP-адрес и пользователя для SSH (обычно `root`).

## Первичное подключение

```bash
ssh root@SERVER_IP
docker --version
docker compose version
```

Если образ Docker не использовался, установите Docker Engine и Compose Plugin по официальной
инструкции Docker для Ubuntu.

## Перенос без потери истории

Перенос нужно делать с остановленным локальным контейнером, чтобы SQLite-база и её WAL-файл
не изменялись во время копирования.

На локальном компьютере из папки проекта:

```bash
docker compose stop bot
ssh root@SERVER_IP 'mkdir -p /opt/freelance-bot/data'
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '**/__pycache__/' \
  ./ root@SERVER_IP:/opt/freelance-bot/
ssh root@SERVER_IP 'chmod 600 /opt/freelance-bot/.env && chmod 700 /opt/freelance-bot/data'
```

Не запускайте после этого локальный контейнер одновременно с серверным: два экземпляра будут
конкурировать за VK Long Poll и могут дублировать уведомления.

## Запуск на сервере

```bash
ssh root@SERVER_IP
cd /opt/freelance-bot
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

В `compose.yaml` уже задано `restart: unless-stopped`, поэтому контейнер автоматически
запустится после перезагрузки VPS и перезапустится после сбоя.

## Обновление проекта

После локальных изменений:

```bash
rsync -az --delete \
  --exclude '.env' \
  --exclude 'data/' \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '**/__pycache__/' \
  ./ root@SERVER_IP:/opt/freelance-bot/
ssh root@SERVER_IP 'cd /opt/freelance-bot && docker compose up -d --build'
```

`.env` и `data/` при обновлении исключены специально: серверные секреты и база не должны
перезаписываться локальными файлами.

## Проверка и резервная копия

```bash
ssh root@SERVER_IP 'cd /opt/freelance-bot && docker compose ps'
ssh root@SERVER_IP 'cd /opt/freelance-bot && docker compose logs --tail=100 bot'
```

Для восстановления состояния нужно сохранять `/opt/freelance-bot/data`. Самый простой вариант —
включить резервные копии или периодические снимки диска в панели REG.RU.
