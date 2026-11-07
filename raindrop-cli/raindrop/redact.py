"""Local secret redaction for raindrop.

Runs on the client during `raindrop sync`, before anything is POSTed to push_messages.
Three layers, applied in order:

1. Known values  - exact secrets we already know (e.g. the raindrop key in config.json).
2. Key-value     - `api_key = "..."`, `Authorization: Bearer ...`, `password: ...`, env-style lines.
3. Known formats - provider token shapes (AWS, GitHub, Stripe, OpenAI, Anthropic, ...), JWTs,
                   PEM private keys, credentials embedded in URLs.

Usage:
    from raindrop.redact import redact, redact_obj
    clean = redact(text, known_secrets=[cfg["key"]])
    clean_exchange = redact_obj(exchange, known_secrets=[cfg["key"]])
"""

from __future__ import annotations

import re
from typing import Any, Iterable

REDACTED = "[REDACTED]"

# Known values shorter than this are ignored; redacting e.g. "abc" would shred normal text.
MIN_KNOWN_SECRET_LEN = 8

# --- Layer 3: known token formats -------------------------------------------------------------
# (label, pattern). Order matters: more specific patterns first.
_FORMAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
            re.DOTALL,
        ),
    ),
    # Truncated/unterminated private key (e.g. cut off by a chunk boundary): redact to end of text.
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*", re.DOTALL)),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+")),
    # user:password@host inside a URL. Keep scheme/user/host, drop the password.
    ("url-credentials", re.compile(r"(?<=://)([^\s/:@]+):([^\s/@]+)(?=@)")),
]

# --- Layer 2: key-value pairs -----------------------------------------------------------------
# Names that imply the value is secret. Matched as a substring of an identifier, so
# `OPENAI_API_KEY`, `db_password`, `clientSecret` all hit.
_SECRET_NAME = (
    r"[A-Za-z0-9_.\-]*"
    r"(?:api[_\-]?key|apikey|secret|token|passwd|password|pwd|credential|private[_\-]?key|"
    r"access[_\-]?key|auth|bearer|cookie)"
    r"[A-Za-z0-9_.\-]*"
)

# name <sep> value, where the name may be quoted (JSON) and the value may be quoted.
_KV_PATTERN = re.compile(
    r"""(?P<prefix>
            (?P<q1>["']?)(?P<name>""" + _SECRET_NAME + r""")(?P=q1)   # optional-quoted name
            \s*(?:=|:|=>)\s*                                           # separator
        )
        (?:
            (?P<q2>["'`])(?P<qval>[^"'`\n]{4,}?)(?P=q2)               # quoted value
          | (?P<val>[^\s"'`,;&(){}\[\]]{6,})(?=[\s"'`,;&)}\]]|$)                            # bare value
        )""",
    re.IGNORECASE | re.VERBOSE,
)

# `Authorization: Bearer xyz` / `Authorization: Basic xyz` / `Bearer xyz` anywhere.
_AUTH_HEADER = re.compile(
    r"(?P<prefix>\b(?:Bearer|Basic|Token)\s+)(?P<val>[A-Za-z0-9._~+/\-]{12,}=*)",
)

# Values that look like prose/identifiers rather than secrets - avoids mangling code such as
# `token = get_token()` or `password: str`.
_NOT_A_SECRET = re.compile(
    r"""^(?:
        (?:true|false|none|null|nil|undefined|string|str|int|bool|required|optional)
        | (?:bearer|basic|token|digest)   # auth scheme word, not the credential
        | //.*                            # tail of a URL (e.g. credential.https://host)
        | [A-Za-z_][A-Za-z0-9_.]*\(.*     # function call
        | \$\{?[A-Za-z_][A-Za-z0-9_]*\}?  # ${ENV_VAR} / $ENV_VAR reference
        | <[^>]*>                          # <placeholder>
        | \[REDACTED[^\]]*\]               # already redacted
        | \*{3,}
        | x{4,}
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _looks_like_secret_value(val: str) -> bool:
    if _NOT_A_SECRET.match(val):
        return False
    # Pure dotted identifiers (e.g. `self.token`, `config.api_key`) are code, not secrets.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", val):
        return False
    return True


def _kv_sub(m: re.Match[str]) -> str:
    val = m.group("qval") if m.group("qval") is not None else m.group("val")
    if not _looks_like_secret_value(val):
        return m.group(0)
    q = m.group("q2") or ""
    return f"{m.group('prefix')}{q}{REDACTED}{q}"


def _auth_sub(m: re.Match[str]) -> str:
    if not _looks_like_secret_value(m.group("val")):
        return m.group(0)
    return f"{m.group('prefix')}{REDACTED}"


def _format_sub(label: str):
    if label == "url-credentials":
        return lambda m: f"{m.group(1)}:{REDACTED}"
    return lambda m: f"[REDACTED:{label}]"


# --- Layer 4: high-entropy fallback -----------------------------------------------------------
_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_\-+=/.])[A-Za-z0-9_\-+=]{32,}(?![A-Za-z0-9_\-+=/.])")
ENTROPY_THRESHOLD = 4.2  # bits/char; random base64 is ~5.5+, English/identifiers ~3-3.8


def _shannon_entropy(s: str) -> float:
    from collections import Counter
    from math import log2

    n = len(s)
    return -sum(c / n * log2(c / n) for c in Counter(s).values())


def _entropy_sub(m: re.Match[str]) -> str:
    s = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]+", s):  # git SHAs, md5/sha digests: not secrets
        return s
    if not (re.search(r"\d", s) and re.search(r"[A-Za-z]", s)):
        return s
    if _shannon_entropy(s) < ENTROPY_THRESHOLD:
        return s
    return "[REDACTED:high-entropy]"


# --- Known values from .env files -------------------------------------------------------------
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>.*?)\s*$")
_ENV_SECRET_NAME = re.compile(r"^" + _SECRET_NAME + r"$", re.IGNORECASE)


def load_env_secrets(path) -> list[str]:
    """Collect secret-looking values from a .env file (or every `.env*` file in a directory).

    Only variables whose *name* looks secret (API_KEY, *_TOKEN, PASSWORD, ...) are returned, so
    ordinary config like `NODE_ENV=production` is not scrubbed from transcripts. Pass the result
    as `known_secrets` to redact()/redact_obj(). Missing/unreadable files yield [].
    """
    from pathlib import Path

    p = Path(path)
    files = sorted(p.glob(".env*")) if p.is_dir() else [p]
    out: list[str] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            m = _ENV_LINE.match(line)
            if not m or not _ENV_SECRET_NAME.match(m.group("name")):
                continue
            val = m.group("val").strip("\"'`")
            if len(val) >= MIN_KNOWN_SECRET_LEN and _looks_like_secret_value(val):
                out.append(val)
    return out


# --- Public API -------------------------------------------------------------------------------


def redact(text: str, known_secrets: Iterable[str] = ()) -> str:
    """Return `text` with secrets replaced by `[REDACTED]` markers.

    `known_secrets` are exact values to scrub wherever they appear (e.g. the local raindrop key,
    or values from the user's .env). Values shorter than MIN_KNOWN_SECRET_LEN are ignored.
    """
    if not text:
        return text

    # 1. Known values - longest first so a secret containing another is fully removed.
    for secret in sorted({s for s in known_secrets if s and len(s) >= MIN_KNOWN_SECRET_LEN}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)

    # 3. Known formats first, so labelled tokens keep their specific label...
    for label, pattern in _FORMAT_PATTERNS:
        text = pattern.sub(_format_sub(label), text)

    # 2. ...then generic key-value / auth-header catch-alls for whatever remains.
    text = _AUTH_HEADER.sub(_auth_sub, text)
    text = _KV_PATTERN.sub(_kv_sub, text)

    # 4. Last resort: long random-looking strings with no recognizable label.
    text = _ENTROPY_CANDIDATE.sub(_entropy_sub, text)
    return text


def redact_obj(obj: Any, known_secrets: Iterable[str] = ()) -> Any:
    """Recursively redact every string in a JSON-like structure (dicts, lists, strings).

    Dict keys are left alone; if a key itself looks secret (e.g. "api_key") and its value is a
    string, the whole value is replaced. Returns a new object; the input is not mutated.
    """
    secrets = tuple(known_secrets)
    name_re = re.compile(r"^" + _SECRET_NAME + r"$", re.IGNORECASE)

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return redact(o, secrets)
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(walk(v) for v in o)
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(k, str) and isinstance(v, str) and name_re.match(k) and _looks_like_secret_value(v) and len(v) >= 6:
                    out[k] = REDACTED
                else:
                    out[k] = walk(v)
            return out
        return o

    return walk(obj)
