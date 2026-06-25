import logging


def setup_logger(
    log_file: str = "bot.log", level: int = logging.DEBUG
) -> logging.Logger:
    logger = logging.getLogger("telegram_keyword_monitor")
    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
    return logger
