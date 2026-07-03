#!/usr/bin/env python3
"""
Разовый скрипт: добавляет/обновляет параметр mtproto_proxy
в существующем зашифрованном config.ini, не трогая остальные поля.

Запуск (из корня проекта):
    python3 add_mtproto_proxy_to_config.py
"""

import configparser
from io import StringIO
from cryptography.fernet import Fernet

CONFIG_PATH = "config.ini"
KEY_PATH = "config.key"

MTPROTO_PROXY_VALUE = (
    "personal3.proxytg.space:8443:"
    "eeda92971cea3524eb834ae5a6a93522c6706572736f6e616c332e70726f787974672e7370616365"
)


def load_key():
    with open(KEY_PATH, "rb") as f:
        return f.read().strip()


def decrypt_config(key: bytes) -> str:
    f = Fernet(key)
    with open(CONFIG_PATH, "rb") as fin:
        data = fin.read()
    if data.startswith(b"ENCRYPTED\n"):
        data = data[len(b"ENCRYPTED\n"):]
        return f.decrypt(data).decode("utf-8")
    return data.decode("utf-8")


def encrypt_config(config_str: str, key: bytes):
    f = Fernet(key)
    enc = f.encrypt(config_str.encode("utf-8"))
    with open(CONFIG_PATH, "wb") as fout:
        fout.write(b"ENCRYPTED\n" + enc)


def main():
    key = load_key()
    config_str = decrypt_config(key)

    config = configparser.ConfigParser(interpolation=None)
    config.read_string(config_str)

    old_value = config["Settings"].get("mtproto_proxy", "(не задан)")
    config["Settings"]["mtproto_proxy"] = MTPROTO_PROXY_VALUE

    buf = StringIO()
    config.write(buf)
    encrypt_config(buf.getvalue(), key)

    print("✅ config.ini обновлён.")
    print(f"   mtproto_proxy: '{old_value}' → '{MTPROTO_PROXY_VALUE}'")


if __name__ == "__main__":
    main()