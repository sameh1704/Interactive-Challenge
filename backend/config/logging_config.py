"""Logging configuration.

The application logs to stdout/stderr so that Docker log drivers capture
everything. The verbosity is driven by the ``LOG_LEVEL`` environment variable.
"""

from __future__ import annotations

from typing import Any

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_LOGGERS: dict[str, str] = {
    "django": "INFO",
    "django.server": "INFO",
    "django.request": "WARNING",
    "django.security": "WARNING",
    "channels.server": "INFO",
    "channels.request": "WARNING",
    "daphne": "INFO",
    "asgi": "INFO",
}


def build_logging_config(level: str, environment: str) -> dict[str, Any]:
    """Return a Django ``LOGGING`` dictionary.

    Args:
        level: Minimum level for the root logger.
        environment: Deployment environment name, added to every record so that
            logs from different environments can be told apart.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "environment": {
                "()": "config.logging_config.EnvironmentFilter",
                "environment": environment,
            }
        },
        "formatters": {
            "console": {
                "format": f"{environment} | " + _CONSOLE_FORMAT,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "console",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            name: {
                "handlers": ["console"],
                "level": logger_level,
                "propagate": False,
            }
            for name, logger_level in _LOGGERS.items()
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    }


class EnvironmentFilter:
    """Attach the deployment environment name to every log record."""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    def filter(self, record) -> bool:  # noqa: A002 - logging API name
        record.environment = self.environment
        return True