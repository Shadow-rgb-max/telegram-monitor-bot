import configparser
from typing import List, Optional, Tuple
from cryptography.fernet import Fernet
import os


class BotConfig:
    def __init__(
        self,
        api_id: str,
        api_hash: str,
        admin_id: int,
        channel_id: str,
        channels: List[str],
        keywords: List[str],
        dedup_window_hours: int = 24,
        mtproto_proxy: Optional[Tuple[str, int, str]] = None,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.admin_id = admin_id
        self.channel_id = channel_id
        self.channels = channels
        self.keywords = keywords
        self.dedup_window_hours = dedup_window_hours
        self.mtproto_proxy = mtproto_proxy  # (host, port, secret)


def load_key(key_path: str = "config.key") -> bytes:
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"Key file {key_path} not found.")
    return open(key_path, "rb").read().strip()


def decrypt_config(enc_path: str, key_path: str = "config.key") -> str:
    key = load_key(key_path)
    f = Fernet(key)
    with open(enc_path, "rb") as f_enc:
        data = f_enc.read()
    if data.startswith(b"ENCRYPTED\n"):
        data = data[len(b"ENCRYPTED\n"):]
        decrypted = f.decrypt(data)
        return decrypted.decode("utf-8")
    else:
        # Not encrypted, treat as plain text
        return data.decode("utf-8")


def parse_mtproto_proxy(proxy_string: str) -> Optional[Tuple[str, int, str]]:
    """
    Парсит строку MTProto прокси в формате host:port:secret
    Возвращает кортеж (host, port, secret) или None если строка пустая
    """
    if not proxy_string or not proxy_string.strip():
        return None

    parts = proxy_string.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid MTProto proxy format. Expected host:port:secret, got: {proxy_string}")

    host = parts[0].strip()
    try:
        port = int(parts[1].strip())
    except ValueError:
        raise ValueError(f"Invalid port in MTProto proxy: {parts[1]}")

    secret = parts[2].strip()

    return (host, port, secret)


def get_config(config_path: str = "config.ini", key_path: str = "config.key") -> BotConfig:
    # Check if file is encrypted
    with open(config_path, "rb") as f:
        first_line = f.readline()
        f.seek(0)
        if first_line.startswith(b"ENCRYPTED"):
            config_str = decrypt_config(config_path, key_path)
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(config_str)
        else:
            config = configparser.ConfigParser(interpolation=None)
            if not config.read(config_path):
                raise FileNotFoundError(f"Config file {config_path} not found or is empty.")
    try:
        api_id = config["Telegram"]["api_id"]
        api_hash = config["Telegram"]["api_hash"]
        admin_id = int(config["Telegram"]["admin_id"])
        channel_id = config["Telegram"]["channel_id"].strip()
        channels = [
            ch.strip() for ch in config["Settings"]["channels"].split(",") if ch.strip()
        ]
        keywords = [
            kw.strip().lower()
            for kw in config["Settings"]["keywords"].split(",")
            if kw.strip()
        ]
        if not channels or not keywords:
            raise KeyError("channels or keywords in config.ini are empty")

        # Читаем настройку окна дедупликации (по умолчанию 24 часа)
        dedup_window_hours = 24
        if "dedup_window_hours" in config["Settings"]:
            try:
                dedup_window_hours = int(config["Settings"]["dedup_window_hours"])
            except ValueError:
                pass  # Используем значение по умолчанию

        # Читаем настройку MTProto прокси (опционально)
        mtproto_proxy = None
        if "mtproto_proxy" in config["Settings"]:
            proxy_str = config["Settings"]["mtproto_proxy"].strip()
            if proxy_str:
                mtproto_proxy = parse_mtproto_proxy(proxy_str)

        return BotConfig(
            api_id, 
            api_hash, 
            admin_id, 
            channel_id, 
            channels, 
            keywords, 
            dedup_window_hours,
            mtproto_proxy
        )
    except KeyError as e:
        raise KeyError(f"Missing required parameter in config.ini: {e}")
    except ValueError as e:
        raise ValueError(f"Invalid parameter format in config.ini: {e}")