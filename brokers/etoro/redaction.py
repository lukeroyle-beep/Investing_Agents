from __future__ import annotations

import logging
import re
from typing import Iterable


_HEADER_SECRET = re.compile(
    r"(?i)(x-(?:api|user)-key\s*[:=]\s*)([^\s,;]+)"
)
_ENV_SECRET = re.compile(
    r"(?i)(ETORO_(?:DEMO|REAL)_(?:API|USER)_KEY\s*[:=]\s*)([^\s,;]+)"
)


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    text = _HEADER_SECRET.sub(r"\1[REDACTED]", text)
    text = _ENV_SECRET.sub(r"\1[REDACTED]", text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


class EtoroSecretFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(str(secret) for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self._secrets)
        record.args = ()
        return True
