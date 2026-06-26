#!/bin/bash
source ./venv/bin/activate

python -c "
import os
os.environ['USE_PROXY_POOL'] = 'false'
os.environ['PROXY_POOL_SIZE'] = '0'
" 


export TELEGRAM_PROXY=socks5://127.0.0.1:10808
export USE_PROXY_POOL=false

python main.py