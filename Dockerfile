# Используем официальный Python-образ (slim версия для меньшего размера и быстрой загрузки)
FROM python:3.11-slim

WORKDIR /app
COPY . /app

# Установка зависимостей Python
RUN pip install --no-cache-dir -r requirements.txt

# Установка supervisor
RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

# Копируем конфиг supervisord внутрь контейнера
COPY supervisord.conf /etc/supervisord.conf

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"] 