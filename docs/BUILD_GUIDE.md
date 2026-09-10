# 📦 Установка hermes-telegram-brain (tg-service)

Полная инструкция: от чистой машины до работающего сервиса с подключённым агентом. Два пути, выбирайте по
железу.

| Ваша машина | Путь | Почему |
|---|---|---|
| 🍎 **Mac на Apple Silicon** | uv напрямую, Postgres в Docker | Whisper через mlx требует доступа к Metal, а контейнер на macOS его не получает. В Docker расшифровка голосовых работать не будет |
| 🐧 **Linux, VPS, сервер** | docker compose, всё в контейнерах | Одна команда, изоляция, автозапуск из коробки |

Время: около часа на Mac, около 40 минут на Linux. Плюс ожидание загрузки моделей.

---

## 1. Что получится

```
Telegram ──► tg-service ──── слушает чаты, догоняет историю, качает медиа,
                 │           отправляет черновики, следит за здоровьем, API :8077
                 ▼
          Postgres 17 + pgvector
                 ▲
                 │
          tg-media-worker ─── голосовые в текст, картинки в описание и OCR,
                 ▲            документы в текст, эмбеддинги для поиска, :8078
                 │
             tg-mcp ───────── MCP-сервер для агента поверх API

Отправка: агент ──► черновик ──► карточка с кнопками в вашу группу ──► ✅ ──► сообщение ушло
```

Два процесса и одна база. Сессией Telegram владеет **только** `tg-service`, второй процесс ходит в базу и
на диск. Поэтому нельзя запускать сервис на двух машинах одновременно с одним файлом сессии.

---

## 2. Что нужно получить заранее

| Что | Зачем | Где взять |
|---|---|---|
| `api_id` и `api_hash` | Доступ к Telegram по протоколу MTProto | [my.telegram.org/apps](https://my.telegram.org/apps), раздел API development tools |
| Аккаунт Telegram | Сервис работает под ним | Ваш обычный. Не чужой |
| Токен бота | Карточки подтверждения с кнопками и ежедневная сводка | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Приватная группа | Куда бот шлёт карточки. Только вы внутри | Создать в Telegram. Бота сервис добавит сам |
| Ключ OpenRouter | Описание и OCR картинок через внешнюю vision-модель. Необязательно. Подойдёт и другой vision-провайдер, см. примечание под таблицей | [openrouter.ai](https://openrouter.ai) |

> 🖼 **Картинки и сканы распознаются внешней vision-моделью** — это не локальный компонент. Сервис шлёт
> изображение (или страницы скана PDF) на API провайдера. По умолчанию — OpenRouter, любая vision-модель
> из его каталога (MiniMax, GPT-4o, Claude, Gemini, ...), выбирается переменной `VISION_MODEL`. Дословный
> OCR входит в ответ модели. Если хотите другого провайдера с vision (свой OpenAI-совместимый endpoint,
> Ollama с vision-моделью) — проще всего через OpenRouter-совместимый шлюз; в коде запрос ходит на
> `https://openrouter.ai/api/v1/chat/completions` (см. `tg_service/media/vision.py`).
>
> Без ключа картинки и сканы просто не распознаются — всё остальное работает. Учтите: содержимое картинок
> уходит на провайдера, поэтому не отправляйте в vision то, что нельзя показывать третьей стороне
> (см. [SECURITY.md](../SECURITY.md)).

Если из вашей сети Telegram или OpenRouter недоступны напрямую, понадобится прокси, смотрите раздел 8.

---

## 3. 🐧 Путь Linux / VPS: docker compose

### 3.1 Требования к серверу

| | Минимум | Комфортно |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 4 ГБ | 8 ГБ |
| Диск | 40 ГБ | 80 ГБ |
| ОС | Ubuntu 22.04+ или Debian 12+ | то же |

Меньше 4 ГБ не берите: Postgres просит около гигабайта, модель распознавания речи ещё около гигабайта.

### 3.2 Подготовка

```bash
sudo apt update && sudo apt install -y git ffmpeg curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # перелогиньтесь после этой команды
```

### 3.3 Установка

```bash
git clone https://github.com/Mobiss11/hermes-telegram-brain.git
cd hermes-telegram-brain
cp .env.example .env
nano .env                       # заполнить TG_API_ID, TG_API_HASH, API_TOKEN
```

Токен API сгенерируйте так:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3.4 Вход в Telegram

Шаг интерактивный, его нужно сделать один раз до запуска сервисов:

```bash
docker compose run --rm app tg-login
```

Спросит телефон, код из Telegram, при включённой двухфакторной аутентификации пароль. Появится
`data/user.session`.

### 3.5 Запуск

```bash
docker compose up -d
docker compose logs -f app
```

В логе должно пройти: применение миграций, `logged in as ...`, `Uvicorn running`, затем `dialogs synced` и
загрузка истории по чатам.

### 3.6 Обновление

```bash
git pull
docker compose build
docker compose up -d
```

---

## 4. 🍎 Путь Mac: uv

### 4.1 Подготовка

```bash
brew install uv git ffmpeg
uv python install 3.12
```

Python строго 3.12: у mlx нет колёс под 3.14.

> ⚠️ **Важно для macOS.** Не кладите проект физически в `~/Documents`, `~/Desktop` или `~/Downloads`.
> Фоновые агенты launchd, открывающие файлы в этих папках, зависают на системном диалоге разрешения,
> которого никто не видит. Положите в `~/hermes-telegram-brain`, а в Documents при желании сделайте симлинк.

### 4.2 Установка

```bash
git clone https://github.com/Mobiss11/hermes-telegram-brain.git ~/hermes-telegram-brain
cd ~/hermes-telegram-brain
uv sync --extra media
cp .env.example .env
```

Флаг `--extra media` тянет mlx-whisper, sentence-transformers с torch и парсеры документов, это несколько
сотен мегабайт. Без него сервис работает, но медиа-воркер не запустится.

Заполните в `.env` как минимум `TG_API_ID`, `TG_API_HASH` и `API_TOKEN`.

### 4.3 База данных

```bash
docker compose up -d db
docker compose exec -T db pg_isready -U tg
```

Поднимется `pgvector/pgvector:pg17` на `127.0.0.1:5436`. Схему создавать не нужно: миграции применяются при
старте сервиса. Если порт занят, поменяйте его в `docker-compose.yml` и в `DATABASE_URL`.

### 4.4 Вход и запуск

```bash
uv run tg-login       # телефон, код, при 2FA пароль
uv run tg-service     # первый терминал
```

Во втором терминале:

```bash
uv run --extra media tg-media-worker
```

### 4.5 Автозапуск через launchd

В папке `deploy/launchd/` лежат два шаблона. Замените в них `__PROJECT_DIR__`, `__USER__` и `__UV__` на свои
значения, затем:

```bash
cp deploy/launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tg-service.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tg-media-worker.plist
```

Перезапуск после обновления кода:

```bash
git pull && uv sync --extra media
launchctl kickstart -k gui/$(id -u)/tg-service
launchctl kickstart -k gui/$(id -u)/tg-media-worker
```

> ⚠️ В шаблонах стоит `uv run --no-sync`, и это обязательно. Без флага два процесса с разными наборами
> зависимостей начнут пересобирать окружение друг у друга и оба зависнут.

---

## 5. 🎙 Распознавание речи: что выбрать

Здесь главное отличие платформ. Текущая версия вызывает `mlx-whisper` напрямую.

| Ваше железо | Модель и бэкенд | Скорость | Память | Как включить |
|---|---|---|---|---|
| 🍎 Apple Silicon M1+ | `mlx-community/whisper-large-v3-turbo` | в разы быстрее реального времени | ~2 ГБ | Работает из коробки |
| 🍎 Apple Silicon, мало памяти | `mlx-community/whisper-small-mlx` | ещё быстрее, качество ниже | ~0.5 ГБ | `WHISPER_MODEL=mlx-community/whisper-small-mlx` |
| 🐧 Linux, 4+ vCPU | `faster-whisper` `small` с `int8` | быстрее реального времени в несколько раз | ~0.5 ГБ | Смотрите примечание ниже |
| 🐧 Linux, 2 vCPU | Внешний API распознавания | зависит от провайдера | 0 | Смотрите примечание ниже |
| 🐧 Любой Linux | Отключить расшифровку | — | 0 | `MEDIA_AUTO_TRANSCRIBE=false` |

**Примечание про Linux.** Пакет `mlx` публикует manylinux-колёса, поэтому установка `--extra media` на
Ubuntu 22.04+ проходит без ошибок, и расшифровка, вероятно, отработает на CPU. Но это **непроверенный
путь**: mlx создавался под Apple Metal, официальной поддержки Linux у mlx-whisper нет, скорость на CPU
будет заметно ниже.

Что делать сейчас, если вы на Linux и голосовые важны:

1. Проверьте, работает ли расшифровка как есть: отправьте себе голосовое, посмотрите
   `GET /media/jobs?status=failed`.
2. Если падает, поставьте `MEDIA_AUTO_TRANSCRIBE=false` и пользуйтесь остальным. Текст, документы, картинки
   и оба поиска работают на Linux полноценно.
3. Выбор бэкенда через переменную `STT_BACKEND` (`mlx` / `faster-whisper` / внешний API) в планах.
   Пока его нет, замена делается правкой одного файла `tg_service/media/transcribe.py`, там 30 строк.

**Про эмбеддинги для поиска по смыслу.** Модель `multilingual-e5-small` работает на обычном CPU без
всяких оговорок, десятки тысяч сообщений индексируются за минуты. На обеих платформах из коробки.

---

## 6. ⚙️ Настройка

Все переменные с комментариями лежат в [.env.example](../.env.example). Здесь только то, без чего не поедет.

**Обязательный минимум:**

| Переменная | Что писать |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | С my.telegram.org |
| `API_TOKEN` | Длинная случайная строка |
| `DATABASE_URL` | Оставьте как есть, если не меняли порт |

**Чтобы заработала отправка с подтверждением:**

| Переменная | Что писать |
|---|---|
| `BOT_TOKEN` | Токен из BotFather |
| `OUTBOX_CHAT` | id вашей приватной группы, например `-1001234567890` |

Узнать id группы проще всего после первого запуска:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" "localhost:8077/chats?q=название-вашей-группы"
```

**Политики по умолчанию**, которые обычно менять не нужно: голосовые и кружки расшифровываются
автоматически, документы до 20 МБ разбираются, фото распознаются, и всё это только в личках и группах.
Каналы автоматика не трогает никогда, видео и крупные файлы качаются только по запросу агента.

---

## 7. ✅ Проверка, что всё работает

```bash
export API_TOKEN=<ваш токен>

curl -s -H "Authorization: Bearer $API_TOKEN" localhost:8077/health
curl -s -H "Authorization: Bearer $API_TOKEN" localhost:8077/health/full
curl -s -H "Authorization: Bearer $API_TOKEN" "localhost:8077/chats?limit=5"
curl -s localhost:8078/health          # медиа-воркер, сколько сообщений проиндексировано
```

**Обработать историю, а не только новое:**

```bash
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"days": 30, "kinds": ["voice", "video_note", "document", "photo"]}' \
  localhost:8077/media/backlog
```

Ход дел: `GET /media/jobs?status=queued|done|failed|skipped`. Статус `skipped` означает, что исходное
сообщение уже удалено в Telegram, это нормально.

**Проверить отправку без агента.** Создайте черновик самому себе:

```bash
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"chat_id": <ваш user id>, "text": "тест", "reason": "проверка"}' \
  localhost:8077/outbox
```

В группе подтверждений появится карточка с кнопками. Нажмите ✅, сообщение уйдёт, карточка обновится.
Работают и текстовые команды: `ok 1`, `no 1`, ответ на карточку своим текстом заменяет текст черновика,
`stop` отключает отправку до перезапуска.

---

## 8. 🌐 Прокси, если Telegram или OpenRouter недоступны

Актуально для серверов и домашних сетей в России и ряде других стран. Сервис умеет SOCKS5, SOCKS4 и HTTP,
и через этот же адрес ходит и к Telegram, и к внешней vision-модели (OpenRouter) — то есть прокси закрывает
сразу оба внешних сервиса: чаты и распознавание картинок.

Простейший туннель, если есть любой VPS за границей:

```bash
ssh -N -D 127.0.0.1:1080 user@ваш-vps
```

Затем в `.env`:

```
TG_PROXY=socks5://127.0.0.1:1080
```

Для постоянной работы заверните туннель в `autossh` или в контейнер с политикой перезапуска. Встроенная
проверка здоровья следит за портом прокси и пишет в группу подтверждений, если он отвалился.

Типичные комбинации:

| Где сервер | Telegram | OpenRouter | Что делать |
|---|---|---|---|
| VPS за пределами РФ | ✅ | ✅ | Прокси не нужен |
| VPS в РФ | ✅ | ❌ | Прокси для OpenRouter или `MEDIA_AUTO_DESCRIBE=false` |
| Домашняя сеть в РФ | зависит от провайдера | ❌ | Прокси |

---

## 9. 📌 Что важно помнить

- **Одна сессия на один файл.** Не запускайте сервис на двух машинах с одним `data/user.session`,
  авторизация сломается.
- **API наружу не выставлять.** Это доступ ко всей переписке. Нужен удалённый доступ — ssh-туннель.
- **Первый запуск заметен для Telegram.** Загрузка истории по сотням чатов это всплеск активности.
  Не увеличивайте `INITIAL_HISTORY` без нужды.
- **Диск.** Скачанные файлы удаляются через `MEDIA_RETENTION_DAYS` дней, извлечённый текст остаётся навсегда.
  При повторном запросе файл скачается заново.
- **Резервная копия.** Дампьте базу, если история для вас ценна:
  `docker compose exec -T db pg_dump -U tg -Fc tg > backup.dump`

---

## 10. Что дальше

- 🔌 Подключить агента: [MCP.md](MCP.md)
- 🏗 Как это устроено внутри: [ARCHITECTURE.md](ARCHITECTURE.md)
- 🔧 Что-то сломалось: [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- 🔐 Риски и модель угроз: [SECURITY.md](../SECURITY.md)
