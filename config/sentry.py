import logging

import sentry_sdk

from config.settings import get_settings

logger = logging.getLogger(__name__)


def init_sentry():
    """Initialize Sentry for logging, error tracking, and monitoring."""
    sentry_dsn = get_settings().sentry_dsn
    if sentry_dsn:
        sentry_sdk.init(
            dsn=sentry_dsn,
            _experiments={
                "enable_logs": True,
            },
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
            # Profiles will be automatically collected while
            # there is an active span.
            profile_lifecycle="trace",
            environment=get_settings().environment,
        )
    else:
        logger.info(
            "Sentry disabled", extra={"reason": "SENTRY_DSN is empty or not set"}
        )
