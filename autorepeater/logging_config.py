"""Logging helpers for local and Yandex Cloud runs."""
import logging

from pythonjsonlogger import jsonlogger

from autorepeater.constants import IMPORTANT

LOGGER_NAME = 'tinkoffBot'


logger = logging.getLogger(LOGGER_NAME)


class YcLoggingFormatter(jsonlogger.JsonFormatter):
    """Format records for Yandex Cloud Functions logs."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record['logger'] = record.name
        log_record['level'] = record.levelname.replace(
            'IMPORTANT', 'INFO').replace(
                'WARNING', 'WARN').replace('CRITICAL', 'FATAL')


def configure_local_logging():
    """Configure default logging for local CLI runs."""
    logging.addLevelName(IMPORTANT, 'IMPORTANT')
    logging.getLogger().setLevel(IMPORTANT)


def configure_yc_logging():
    """Configure JSON logging expected by Yandex Cloud Functions."""
    logging.addLevelName(IMPORTANT, 'IMPORTANT')
    if not logger.handlers:
        log_handler = logging.StreamHandler()
        log_handler.setFormatter(
            YcLoggingFormatter('%(message)s %(level)s %(logger)s'))
        logger.addHandler(log_handler)
    logger.propagate = False
    logger.setLevel(IMPORTANT)
