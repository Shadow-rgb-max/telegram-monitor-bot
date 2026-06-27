#!/bin/bash
# Только для бота — отдельный Xray на порту 10810
export TELEGRAM_PROXY=socks5://127.0.0.1:10810
export USE_PROXY_POOL=false

cd /home/lentum/telegram-monitor-bot
python main.py