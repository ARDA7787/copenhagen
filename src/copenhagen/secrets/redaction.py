"""Prevent released worker credentials from appearing in formatted logs or traces."""

import logging

_values: set[str] = set()


def register(secret: str) -> None:
    if secret:
        _values.add(secret)


def redact(message: str) -> str:
    for value in sorted(_values, key=len, reverse=True):
        message = message.replace(value, "[REDACTED]")
    return message


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
