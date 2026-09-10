# 🔧 Что ломается и как чинить

Собрано из реальной эксплуатации. Сначала общая диагностика, потом конкретные симптомы.

---

## С чего начинать всегда

```bash
export API_TOKEN=<ваш токен>

curl -s -H "Authorization: Bearer $API_TOKEN" localhost:8077/health/full   # все проверки разом
curl -s localhost:8078/health                                             # медиа-воркер
curl -s -H "Authorization: Bearer $API_TOKEN" "localhost:8077/media/jobs?status=failed"
```

Логи: `data/service.log` и `data/media-worker.log`, либо `docker compose logs -f app worker`.

---

## Установка и запуск

### `uv sync` падает на mlx-whisper

Вы не на Apple Silicon, либо у вас Python не 3.12. Проверьте `python3 --version` и `uv python list`.
На Linux с glibc старее 2.35 (Ubuntu 20.04 и раньше) колёс нет: обновите систему или ставьте без
`--extra media`.

### Сервис запущен, но лог пустой и процесс висит

**Только macOS.** Проект лежит физически в `~/Documents`, `~/Desktop` или `~/Downloads`. Фоновый агент
launchd упёрся в системный диалог разрешения, которого никто не видит. Перенесите папку в `~/hermes-telegram-brain`,
в Documents оставьте симлинк, если нужно.

Проверить: `ps aux | grep tg-service` покажет процесс `uv` без дочернего python.

### Два процесса uv висят, окружение постоянно пересобирается

В команде запуска нет `--no-sync`. Сервис и медиа-воркер используют разные наборы зависимостей и
пересобирают venv друг у друга. Добавьте флаг во все юниты и в конфиг MCP:

```
uv run --no-sync tg-service
```

### `database "tg" does not exist` или отказ соединения

Контейнер не поднялся или занят порт. Проверьте:

```bash
docker compose ps
docker compose exec -T db pg_isready -U tg
lsof -nP -iTCP:5436 -sTCP:LISTEN
```

Если порт занят другим проектом, поменяйте его в `docker-compose.yml` и в `DATABASE_URL`.

---

## Telegram

### `ConnectionError: Connection to Telegram failed`

Telegram недоступен из вашей сети. Проверьте:

```bash
curl -s -m 8 -o /dev/null -w "%{http_code}\n" https://web.telegram.org/
```

Если ноль, нужен прокси: `TG_PROXY=socks5://127.0.0.1:1080`, смотрите раздел 8 в BUILD_GUIDE.

### `The authorization key is invalid` или постоянные разлогины

Один файл сессии используется двумя процессами или двумя машинами. Оставьте один экземпляр, при
необходимости пройдите `tg-login` заново.

### SMS-код не приходит при `tg-login`

Telegram шлёт код не в SMS, а в само приложение Telegram на другом устройстве. Проверьте там.

### В логе `catch-up failed: The channel specified is private`

Вы вышли из канала или вас удалили. Сервис такие чаты просто пропускает, чинить нечего.

---

## Медиа

### Голосовые не расшифровываются

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" "localhost:8077/media/jobs?status=failed"
```

Смотрите поле `error`:

- `ModuleNotFoundError: mlx_whisper` — установлено без `--extra media`.
- Ошибки mlx на Linux — ожидаемо, смотрите раздел про Whisper в BUILD_GUIDE. Временное решение:
  `MEDIA_AUTO_TRANSCRIBE=false`.
- `ffmpeg not found` — поставьте ffmpeg.

### Много задач в статусе `failed` с `message/media not available`

Обычно спам-чаты: администраторы удаляют сообщения раньше, чем мы успеваем скачать. Такие задачи должны
помечаться как `skipped` и в алерты не попадать. Если их сотни, отключите сбор в этом чате:

```bash
curl -s -X PATCH -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"is_tracked": false}' localhost:8077/chats/<id>/track
```

### Картинки не распознаются

Не задан `OPENROUTER_API_KEY`, либо OpenRouter недоступен из вашей сети. Проверьте:

```bash
curl -s -m 8 -o /dev/null -w "%{http_code}\n" https://openrouter.ai/api/v1/models
```

Ответ 403 или ноль означает блокировку, нужен `TG_PROXY`. Не нужны картинки — поставьте
`MEDIA_AUTO_DESCRIBE=false`.

### Диск заполняется

Проверьте `MEDIA_RETENTION_DAYS` и объём:

```bash
du -sh data/media
```

Автоматика видео не качает, но если агент выкачал их по запросу, они займут место до ротации.

### Семантический поиск возвращает 503

Медиа-воркер не запущен: именно он векторизует поисковый запрос. Проверьте `curl localhost:8078/health`.

---

## Отправка

### Карточки не приходят

1. `BOT_TOKEN` задан?
2. `OUTBOX_CHAT` указывает на существующую группу? Со значением `me` бот работать не может, кнопок не будет.
3. В логе есть строка `outbox prompts and digest are posted by bot @...`? Если вместо неё ошибка про
   добавление бота в чат, добавьте бота в группу руками.
4. У бота не должен быть установлен webhook, иначе он не получит нажатия кнопок:
   `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"`, поле `url` должно быть пустым.

### Нажимаю кнопку, ничего не происходит

Нажатия принимаются только от владельца аккаунта, под которым работает сервис. Если в группе есть кто-то
ещё, его нажатия будут отклонены с сообщением. Проверьте статус:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" localhost:8077/outbox/<id>
```

### Черновик в статусе `expired`

Прошло больше `SEND_DRAFT_TTL_MINUTES` минут. Попросите агента создать новый.

### Отправка выключилась сама

Кто-то написал `stop` в чат подтверждений. Это выключает отправку до перезапуска сервиса, так задумано.

---

## Агент

### Агент не видит инструменты

Смотрите раздел «Если агент не видит инструменты» в [MCP.md](MCP.md). Чаще всего: не полный путь к `uv`,
не совпадает токен, или клиент не перезапущен после правки конфига.

### Агент вызывает инструмент с пустыми аргументами

Особенность слабых моделей при отложенной загрузке инструментов: аргументы теряются на уровне обёртки.
Помогает модель классом выше.

### Агент лезет в API через curl вместо инструментов

Значит инструменты ему недоступны. В Hermes проверьте `platform_toolsets`.

---

## Диагностика для отчёта об ошибке

Если собираетесь открывать issue, приложите:

```bash
uname -a
python3 --version
uv --version
docker compose ps
curl -s -H "Authorization: Bearer $API_TOKEN" localhost:8077/health/full
tail -50 data/service.log
```

**Уберите из логов токены и телефоны перед публикацией.**
