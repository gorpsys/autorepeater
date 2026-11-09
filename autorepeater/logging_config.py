"""Logging helpers for local and Yandex Cloud runs."""
import logging
import os

from pythonjsonlogger import jsonlogger

from autorepeater.constants import IMPORTANT

LOGGER_NAME = 'tinkoffBot'


logger = logging.getLogger(LOGGER_NAME)


class YcLoggingFormatter(jsonlogger.JsonFormatter):
    """Format records for Yandex Cloud Functions logs."""

    def add_fields(self, log_record: dict[str, object], record: logging.LogRecord,
                   message_dict: dict[str, object]) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record['logger'] = record.name
        log_record['level'] = record.levelname.replace(
            'IMPORTANT', 'INFO').replace(
                'WARNING', 'WARN').replace('CRITICAL', 'FATAL')


def _application_level() -> int:
    """Use the same explicit application verbosity for CLI and cloud runs."""
    value = os.environ.get('LOG_LEVEL', 'INFO').strip().upper()
    if value not in ('INFO', 'DEBUG'):
        raise ValueError('LOG_LEVEL must be INFO or DEBUG')
    return logging.DEBUG if value == 'DEBUG' else logging.INFO


def configure_local_logging() -> None:
    """Configure default logging for local CLI runs."""
    level = _application_level()
    logging.addLevelName(IMPORTANT, 'IMPORTANT')
    logging.basicConfig(level=IMPORTANT)
    logging.getLogger().setLevel(IMPORTANT)
    logger.setLevel(level)


def configure_yc_logging() -> None:
    """Configure JSON logging expected by Yandex Cloud Functions."""
    level = _application_level()
    logging.addLevelName(IMPORTANT, 'IMPORTANT')
    if not logger.handlers:
        log_handler = logging.StreamHandler()
        log_handler.setFormatter(
            YcLoggingFormatter('%(message)s %(level)s %(logger)s'))
        logger.addHandler(log_handler)
    logger.propagate = False
    logger.setLevel(level)
