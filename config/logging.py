import logging
import logging.config
import sys

# Unified log format constants to avoid duplication
LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Python dictionary configuration equivalent to the YAML
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": LOG_FORMAT, "datefmt": LOG_DATE_FORMAT},
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
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
