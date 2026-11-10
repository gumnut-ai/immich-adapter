import logging
import logging.config
import sys

from config.telemetry import redact_sensitive_cdn_url, redact_session_tokens_text

# Unified log format constants to avoid duplication
LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RedactSensitiveHttpxQuery(logging.Filter):
    """Redact signed-CDN secrets from HTTPX's automatic request log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx" or not isinstance(record.args, tuple):
            return True
        if len(record.args) < 2:
            return True

        redacted_url = redact_sensitive_cdn_url(str(record.args[1]))
        if redacted_url != str(record.args[1]):
            args = list(record.args)
            args[1] = redacted_url
            record.args = tuple(args)
        return True


class RedactSessionTokensFormatter(logging.Formatter):
    """Replace session tokens in formatted output, tracebacks included.

    Covers Redis keys and session-route paths, such as the server access log of
    `/api/sessions/<token>`. Sentry captures records before formatting and
    scrubs them in its own hooks (`config.sentry`).
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_session_tokens_text(super().format(record))


# Python dictionary configuration equivalent to the YAML
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "class": "config.logging.RedactSessionTokensFormatter",
            "format": LOG_FORMAT,
            "datefmt": LOG_DATE_FORMAT,
        },
    },
    "filters": {
        "redact_sensitive_httpx_query": {"()": RedactSensitiveHttpxQuery},
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "filters": ["redact_sensitive_httpx_query"],
        },
    },
    "loggers": {
        "uvicorn.error": {"level": "INFO", "handlers": ["default"], "propagate": False},
        "uvicorn.access": {
            "level": "INFO",
            "handlers": ["default"],
            "propagate": False,
        },
        "uvicorn": {"level": "INFO", "handlers": ["default"], "propagate": False},
        "watchfiles": {"level": "INFO", "handlers": ["default"], "propagate": False},
        "watchfiles.main": {
            "level": "INFO",
            "handlers": ["default"],
            "propagate": False,
        },
        "fastapi": {"level": "INFO", "handlers": ["default"], "propagate": False},
    },
    "root": {
        "level": "INFO",
        "handlers": ["default"],
    },
}


def init_logging():
    """Initialize logging based on the LOGGING_CONFIG dictionary"""
    logging.config.dictConfig(LOGGING_CONFIG)
