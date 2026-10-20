"""Local rotating logs with credential redaction and no HTTP request logging."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TIKTOK_ACCESS_TOKEN'):
            value = os.environ.get(name)
            if value:
                text = text.replace(value, '[REDACTED]')
        text = re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot[REDACTED]', text)
        text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', text)
        return text


def configure_logging(directory, level='INFO'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / 'rubup.log', maxBytes=2_000_000,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger('rubup')
    for old in logger.handlers[:]:
        old.close()
        logger.removeHandler(old)
    logger.addHandler(handler)
    logger.setLevel(level)
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return logger

