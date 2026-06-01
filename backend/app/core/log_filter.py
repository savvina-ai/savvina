# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Uvicorn access-log filters — suppress health-check noise and redact credentials."""

from __future__ import annotations

import logging
import re


class HealthCheckFilter(logging.Filter):
    """Drop uvicorn access-log records for /health polling requests.

    Docker HEALTHCHECK probes fire every ~30 seconds and create significant
    noise in log aggregators. Filter them at the logger level so they never
    reach any sink (stdout, file, or external collector).
    """

    _re = re.compile(r'"[A-Z]+ /health[\s?]')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._re.search(record.getMessage())


class SensitiveQueryParamFilter(logging.Filter):
    """Redact credential-bearing query parameters from uvicorn access logs.

    OAuth codes, API keys, JWT tokens and session-state values must never
    appear in plain text in any log sink. This filter rewrites the log
    record in-place before it is formatted, so the raw value is never
    serialised anywhere.

    Handled params: code, api_key, token, session_state
    """

    _pattern = re.compile(
        r"([?&])(code|api_key|token|session_state)=([^&\s]+)",
        re.IGNORECASE,
    )
    _replacement = r"\1\2=[REDACTED]"

    def _redact(self, text: str) -> str:
        return self._pattern.sub(self._replacement, text)

    def filter(self, record: logging.LogRecord) -> bool:
        # record.args may be a tuple of formatting arguments
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        return True
