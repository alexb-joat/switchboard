"""Security helpers: auth, sensitive-content detection, safe logging."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re

from .envvars import env
from .config import Privacy

# Built-in detectors for data that should never leave the machine by accident.
# Deliberately conservative: a false positive only means "use a local model".
_BUILTIN = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"),
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "api_key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
        r"|ghp_[A-Za-z0-9]{36}|xox[bpas]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b"
    ),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password_assignment": re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"),
}


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def detect_sensitive(text: str, privacy: Privacy) -> list[str]:
    """Return the names of detectors that fired (never the matched text)."""
    if not privacy.enabled or not text:
        return []
    hits: list[str] = []
    if privacy.builtin_detectors:
        for name, rx in _BUILTIN.items():
            if name == "card_number":
                if any(_luhn_ok(m.group()) for m in rx.finditer(text)):
                    hits.append(name)
            elif rx.search(text):
                hits.append(name)
    low = text.lower()
    for kw in privacy.keywords:
        if kw in low:
            hits.append(f"keyword:{kw}")
    for i, rx in enumerate(privacy.patterns):
        if rx.search(text):
            hits.append(f"pattern:{i}")
    return hits


def fingerprint(text: str) -> str:
    """Short, non-reversible id so logs can correlate requests without content."""
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def router_api_key() -> str | None:
    return env("API_KEY") or None


def check_bearer(header: str | None, expected: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].encode(), expected.encode())


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_safe_bind(host: str) -> None:
    """Refuse to listen beyond loopback without an API key configured."""
    if is_loopback(host):
        return
    key = router_api_key()
    if not key:
        raise SystemExit(
            f"Refusing to bind {host}: set TRIODITIS_API_KEY before exposing the "
            "router beyond localhost."
        )
    if len(key) < 32:
        raise SystemExit("TRIODITIS_API_KEY must be at least 32 characters.")
