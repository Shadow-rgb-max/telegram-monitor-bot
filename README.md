# Telegram Keyword Monitor Bot

## Description (English)

**Telegram Keyword Monitor Bot** is a bot for monitoring messages in specified Telegram channels and automatically notifying the administrator when specified keywords are detected. The bot is based on the Telethon library and supports flexible configuration via a config file.

### Quick Start (English)

1. **Create a virtual environment:**
   ```bash
   python3 -m venv venv
   ```
2. **Activate the virtual environment:**
   - For Linux/macOS:
     ```bash
     source venv/bin/activate
     ```
   - For Windows:
     ```cmd
     venv\Scripts\activate
     ```
3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
4. **Create and configure `config.ini` (see below).**
5. **Run the bot:**
   ```bash
   python telegram_keyword_monitor_bot.py
   ```

## Security Recommendations

- **Never commit your `config.ini` or any files containing `api_id` and `api_hash` to public repositories.**
- Use environment variables or secret managers for sensitive data in production.
- Restrict file permissions for `config.ini` so only the bot user can read it:
  ```bash
  chmod 600 config.ini
  ```
- Consider using `.env` files and libraries like `python-dotenv` for secret management.

## Описание

**Telegram Keyword Monitor Bot** — это бот для мониторинга сообщений в указанных Telegram-каналах и автоматического уведомления администратора при обнаружении заданных ключевых слов. Бот работает на базе библиотеки Telethon и поддерживает гибкую настройку через конфигурационный файл.

## Основные возможности

- Мониторинг нескольких каналов Telegram.
- Поиск сообщений по списку ключевых слов (в том числе с поддержкой вариаций написания, например, "100%", "100 %", "100-%").
- **Дедупликация сообщений** — автоматическая фильтрация одинаковых сообщений из разных каналов.
- Отправка уведомлений администратору с подробной информацией о найденном сообщении.
- Ведение логов работы бота.

## Настройка

В корне проекта должен находиться файл `config.ini` следующей структуры:

```ini
[Telegram]
api_id = <ваш_api_id>
api_hash = <ваш_api_hash>
admin_id = <ваш_telegram_id>
channel_id = <id_или_username_канала_для_уведомлений>

[Settings]
channels = <список_каналов_через_запятую>
keywords = <список_ключевых_слов_через_запятую>
dedup_window_hours = 24
```

**Пояснения к параметрам:**
- `api_id`, `api_hash` — параметры вашего приложения Telegram (получить можно на https://my.telegram.org).
- `admin_id` — ваш Telegram user ID (используется для обратной совместимости, но уведомления теперь отправляются в канал).
- `channel_id` — id (например, `-1001234567890`) или username (например, `@your_channel`) канала, в который бот будет отправлять уведомления. Бот должен быть администратором этого канала.
- `channels` — список каналов для мониторинга (например, `@channel1, @channel2`).
- `keywords` — список ключевых слов для поиска (например, `100 %, 100%, бесплатно`).
- `dedup_window_hours` — окно дедупликации в часах (по умолчанию 24). Сообщения с одинаковым текстом из разных каналов будут отправлены только один раз в течение этого времени.

## Логика работы

- Бот авторизуется в Telegram и проверяет доступ к указанным каналам.
- Для каждого нового сообщения в отслеживаемых каналах бот проверяет наличие ключевых слов.
- **Дедупликация:** Если сообщение с таким же текстом уже было отправлено в течение окна дедупликации, уведомление не отправляется.
- Если ключевое слово найдено и сообщение не является дубликатом, в указанный канал (`channel_id`) отправляется уведомление с текстом сообщения и ссылкой на него.
- Все действия и ошибки логируются в файл `bot.log` и выводятся в консоль.

## Логирование

Вся информация о работе и ошибках сохраняется в файл `bot.log` в корне проекта.

## Завершение работы

Для корректного завершения работы используйте сочетание клавиш `Ctrl+C`. Бот корректно отключится от Telegram.

## Примечания

- Для корректной работы убедитесь, что ваш аккаунт Telegram и сам бот имеют доступ к отслеживаемым каналам.
- Бот должен быть администратором канала, в который отправляются уведомления (`channel_id`).
- Если изменился список каналов или ключевых слов, перезапустите бота.

## Запуск через Docker

1. Соберите образ:
   ```bash
   docker build -t tg-keyword-monitor .
   ```
2. Запустите контейнер:
   ```bash
   docker run --rm -v $(pwd)/config.ini:/app/config.ini tg-keyword-monitor
   ```

## CI/CD

В проекте настроен GitHub Actions для автоматического запуска тестов при каждом коммите.

## Шифрование config.ini

Для защиты конфиденциальных данных используйте шифрование файла config.ini:

1. Сгенерируйте ключ:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > config.key
   ```
2. Зашифруйте config.ini:
   ```python
   from cryptography.fernet import Fernet
   key = open('config.key', 'rb').read()
   f = Fernet(key)
   with open('config.ini', 'rb') as fin:
       data = fin.read()
   enc = f.encrypt(data)
   with open('config.ini', 'wb') as fout:
       fout.write(b'ENCRYPTED\n' + enc)
   ```
3. Для расшифровки бот автоматически использует config.key. Не публикуйте этот ключ!

Если файл начинается с ENCRYPTED, он будет расшифрован автоматически. Для обычного (незашифрованного) файла ничего делать не нужно. 
