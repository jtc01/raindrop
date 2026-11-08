"""Local secret redaction for raindrop.

Runs on the client during `raindrop sync`, before anything is POSTed to push_messages.
Three layers, applied in order:

1. Known values  - exact secrets we already know (e.g. the raindrop key in config.json).
2. Key-value     - `api_key = "..."`, `Authorization: Bearer ...`, `password: ...`, env-style lines.
3. Known formats - provider token shapes (AWS, GitHub, Stripe, OpenAI, Anthropic, ...), JWTs,
                   PEM private keys, credentials embedded in URLs.
4. High entropy  - long random-looking strings with no recognizable label.

A separate layer, `scrub_local_identifiers`, removes *where you are* rather than *what you
know*: absolute paths, your OS username and your hostname, which otherwise reach teammates in
user_text, assistant_text and tool_summaries (files_touched is normalized by the adapters).

Usage:
    from raindrop.redact import LocalIdentifiers, redact, redact_obj, scrub_obj
    clean = redact(text, known_secrets=[cfg["key"]])
    clean_exchange = redact_obj(exchange, known_secrets=[cfg["key"]])
    local = LocalIdentifiers.detect(repo_root=config.repo_root(session.cwd))
    scrubbed, rewrites = scrub_obj(exchange, local)   # run before redact_obj
"""

from __future__ import annotations

import functools
import getpass
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, NamedTuple

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
    # `auth` only as a word (auth, oauth, auth_key, authorization, authentication), never as the
    # start of another word: "Co-Authored-By" and "author" are not secret names.
    r"access[_\-]?key|auth(?:orization|entication|[_\-]?(?:key|code|header))?(?![a-z])|bearer|cookie)"
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
    # `known_secrets=secrets`, `token=token`: the value is a word from the name itself,
    # i.e. a variable or placeholder, not a credential.
    bare = val.lower().rstrip("s")
    if bare and re.fullmatch(r"[a-z_]+", val.lower()) and bare in m.group("name").lower():
        return m.group(0)
    q = m.group("q2") or ""
    return f"{m.group('prefix')}{q}{REDACTED}{q}"


def _auth_sub(m: re.Match[str]) -> str:
    if not _looks_like_secret_value(m.group("val")):
        return m.group(0)
    return f"{m.group('prefix')}{REDACTED}"


def _format_sub(label: str):
    if label == "url-credentials":
        # `user:pass@host` in docs/examples: obvious placeholder passwords are left alone.
        placeholders = {"pass", "password", "passwd", "secret", "user", "xxx", "xxxx", "redacted"}
        return lambda m: (
            m.group(0) if m.group(2).lower() in placeholders else f"{m.group(1)}:{REDACTED}"
        )
    return lambda m: f"[REDACTED:{label}]"


# --- Layer 4: high-entropy fallback -----------------------------------------------------------
# A token touching a path separator (/ or \) is a path segment such as a folder name, not a secret.
_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_\-+=/.\\])[A-Za-z0-9_\-+=]{32,}(?![A-Za-z0-9_\-+=/.\\])")
ENTROPY_THRESHOLD = 4.2  # bits/char; random base64 is ~5.5+, English/identifiers ~3-3.8


def _shannon_entropy(s: str) -> float:
    from collections import Counter
    from math import log2

    n = len(s)
    return -sum(c / n * log2(c / n) for c in Counter(s).values())


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SRI_HASH = re.compile(r"sha(?:1|256|384|512)-[A-Za-z0-9+/_\-]+=*")  # package-lock "integrity" values


def _is_benign_shape(s: str, before: str) -> bool:
    """Long random-looking strings that are known not to be secrets."""
    if re.fullmatch(r"[0-9a-fA-F]+", s):  # git SHAs, md5/sha digests
        return True
    if _UUID.fullmatch(s):
        return True
    if _SRI_HASH.fullmatch(s):
        return True
    if before.endswith("base64,"):  # data: URI payload (images, fonts, ...)
        return True
    return False


def _log_entropy_hit(s: str) -> None:
    """Opt-in diagnostics (RAINDROP_REDACT_LOG=1): record the *shape* of what the entropy
    layer redacted, never the string itself, to ~/.raindrop/redact.log. Never raises."""
    if os.environ.get("RAINDROP_REDACT_LOG") != "1":
        return
    try:
        home = Path(os.environ.get("RAINDROP_HOME", Path.home() / ".raindrop"))
        home.mkdir(parents=True, exist_ok=True)
        shape = "".join(
            sorted({"U" if c.isupper() else "l" if c.islower() else "9" if c.isdigit() else "-" for c in s})
        )
        with (home / "redact.log").open("a", encoding="utf-8") as f:
            f.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} high-entropy len={len(s)} "
                f"entropy={_shannon_entropy(s):.2f} classes={shape}\n"
            )
    except Exception:
        pass


def _entropy_sub(m: re.Match[str]) -> str:
    s = m.group(0)
    if _is_benign_shape(s, m.string[max(0, m.start() - 8) : m.start()]):
        return s
    if not (re.search(r"\d", s) and re.search(r"[A-Za-z]", s)):
        return s
    if _shannon_entropy(s) < ENTROPY_THRESHOLD:
        return s
    _log_entropy_hit(s)
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


# --- Local identifiers: paths, username, hostname ---------------------------------------------
#
# Not secrets, but still nobody else's business: "/Users/matt/Development/raindrop/api/app.py"
# tells every teammate (and every teammate's agent) your username and how your disk is laid
# out, and is useless to them as a path anyway. Paths inside the session's repo become
# repo-relative, so a Mac and a Windows teammate's exchanges talk about the same
# "api/app.py"; anything else under the home directory becomes "~/..."; the username and
# hostname are replaced only where they act as identifiers (path segments, user@host, shell
# prompts), never where the same word appears in prose.

# Characters that may continue a path once we have matched its prefix: anything up to
# whitespace, a quote, or a bracket/separator that ends a shell word.
_PATH_CHARS = r"""[^\s"'`<>|;,()\[\]{}]"""
# A prefix is only a prefix if a separator or the end of the word follows; this is what keeps
# "/x/raindrop" from matching inside "/x/raindrop-old".
_WORD_END = r"(?![\w.\-+~%@$])"
# ...and only if nothing path-like comes before it ("./home/x", "~/Users/x", "/opt/home/x").
_WORD_START = r"(?<![\w.\-~\\/:])"
# A matched prefix must be followed by a separator or by the end of the word.
_PREFIX_END = r"(?:(?=[\\/])|" + _WORD_END + ")"


class Rewrite(NamedTuple):
    """One local identifier that was replaced; `field` is the exchange field it was in."""

    field: str
    original: str
    replacement: str


@dataclass(frozen=True)
class LocalIdentifiers:
    """What to scrub for the machine running the sync. All fields optional: a missing one just
    disables that rule. `windows` selects path rules (case-insensitive, either slash) so the
    pure function can be tested with foreign paths on any OS."""

    repo_root: str | None = None
    home: str | None = None
    username: str | None = None
    hostname: str | None = None
    windows: bool = False

    @classmethod
    def detect(cls, repo_root: str | os.PathLike | None = None) -> "LocalIdentifiers":
        """Identifiers for this machine. Never raises: anything we can't find is left None."""
        home: str | None
        try:
            home = str(Path.home())
        except (RuntimeError, OSError):
            home = None
        username = os.environ.get("USER") or os.environ.get("USERNAME")
        if not username:
            try:
                username = getpass.getuser()
            except Exception:
                username = Path(home).name if home else None
        try:
            hostname = socket.gethostname() or None
        except OSError:
            hostname = None
        return cls(
            repo_root=str(repo_root) if repo_root else None,
            home=home,
            username=username or None,
            hostname=hostname,
            windows=os.name == "nt",
        )


def _path_regex(path: str, windows: bool) -> str:
    """Regex for an absolute path written with either slash; case-insensitive on Windows."""
    segments = [re.escape(seg) for seg in path.replace("\\", "/").rstrip("/").split("/")]
    if windows and re.fullmatch(r"[A-Za-z]:", path[:2]):
        # "C:\\Users\\x", "/c/Users/x" (Git Bash), or drive-less "\\Users\\x" (same drive).
        segments[0] = rf"(?:{segments[0]}|[\\/]{path[0]})?"
    body = r"[\\/]+".join(segments)
    return f"(?i:{body})" if windows else body


def _ident_regex(word: str, windows: bool) -> str:
    return f"(?i:{re.escape(word)})" if windows else re.escape(word)


def _tilde_forms(path: str, home: str | None, windows: bool) -> list[str]:
    """`path` plus, if it is under `home`, the same path written as ~/..."""
    forms = [path]
    if home:
        p, h = path.replace("\\", "/").rstrip("/"), home.replace("\\", "/").rstrip("/")
        if windows:
            p, h = p.lower(), h.lower()
        if p.startswith(h + "/"):
            forms.append("~" + path.replace("\\", "/").rstrip("/")[len(h):])
    return forms


_GENERIC_HOSTNAMES = {"localhost", "localhost.localdomain"}


def _hostname_forms(hostname: str) -> tuple[list[str], list[str]]:
    """(forms replaced everywhere, forms replaced only after '@').

    "Matts-MacBook-Pro.local", "DESKTOP-4F2K9", "zm-laptop" are unmistakably machine names.
    A plain lowercase word ("ubuntu", "mint", "dev") is too likely to be prose, so it is only
    treated as a hostname where a shell prompt or ssh target puts one: right after "user@"."""
    forms: list[str] = []
    for form in (hostname, hostname.split(".")[0]):
        if form and len(form) >= 3 and form.lower() not in _GENERIC_HOSTNAMES and form not in forms:
            forms.append(form)
    everywhere = [f for f in forms if not re.fullmatch(r"[a-z]+", f)]
    prompt_only = [f for f in forms if f not in everywhere]
    return everywhere, prompt_only


@functools.lru_cache(maxsize=16)
def _compile_local(ids: LocalIdentifiers) -> list[tuple[re.Pattern[str], Any]]:
    """(pattern, replacement) pairs, applied in order. Replacements are strings or callables."""
    rules: list[tuple[re.Pattern[str], Any]] = []
    w = ids.windows

    # 1. Paths inside the repo -> repo-relative ("/Users/x/dev/proj/api/app.py" -> "api/app.py",
    #    "~/dev/proj/api/app.py" -> "api/app.py"); the bare root -> "<repo>".
    if ids.repo_root and ids.repo_root.replace("\\", "/").strip("/"):
        roots = "|".join(_path_regex(f, w) for f in _tilde_forms(ids.repo_root, ids.home, w))

        def repo_sub(m: re.Match[str]) -> str:
            rest = m.group("rest")
            if not rest:
                return "<repo>"
            return rest.replace("\\", "/") if w else rest

        rules.append((
            re.compile(_WORD_START + rf"(?:{roots})(?:[\\/]+(?P<rest>{_PATH_CHARS}*))?" + _WORD_END),
            repo_sub,
        ))

    # 2. Anything else under the home directory -> "~" + the rest, separators untouched.
    if ids.home and ids.home.replace("\\", "/").strip("/"):
        rules.append((
            re.compile(_WORD_START + _path_regex(ids.home, w) + _PREFIX_END),
            "~",
        ))

    if ids.username:
        user = _ident_regex(ids.username, w)
        # 3. The username as the home-directory segment of any path, however the path is
        #    written: /Users/<u>/..., /home/<u>/..., C:\Users\<u>\..., c:/users/<u>/..., Git Bash's
        #    /c/Users/<u>/..., and the "-Users-<u>-..." form some tools use in their own file names.
        rules.append((
            re.compile(
                r"(?<![\w.~])" + rf"(?P<prefix>(?:[A-Za-z]:|[\\/][A-Za-z])?(?P<sep>[\\/\-])(?i:Users|home)(?P=sep)){user}"
                + r"(?:(?=[\\/\-])|" + _WORD_END + ")"
            ),
            r"\g<prefix><user>",
        ))
        # 4. user@host, user@ip, shell prompts: "matt@MacBook-Pro ~ %".
        rules.append((re.compile(rf"(?<![\w.\-]){user}(?=@)"), "<user>"))

    if ids.hostname:
        everywhere, prompt_only = _hostname_forms(ids.hostname)
        hosts = "|".join(re.escape(h) for h in sorted(everywhere, key=len, reverse=True))
        if hosts:
            rules.append((re.compile(rf"(?<![\w.\-])(?i:{hosts})(?![\w\-])"), "<host>"))
        hosts = "|".join(re.escape(h) for h in sorted(prompt_only, key=len, reverse=True))
        if hosts:
            rules.append((re.compile(rf"(?<=@)(?:{hosts})(?![\w\-])"), "<host>"))

    return rules


def scrub_local_identifiers(
    text: str, ids: LocalIdentifiers, rewrites: list[Rewrite] | None = None, field: str = ""
) -> str:
    """Return `text` with this machine's paths, username and hostname replaced (see module
    docstring). Each replacement is appended to `rewrites` when given, for `sync --dry-run`."""
    if not text:
        return text
    for pattern, repl in _compile_local(ids):
        if rewrites is None:
            text = pattern.sub(repl, text)
            continue

        def record(m: re.Match[str]) -> str:
            out = repl(m) if callable(repl) else m.expand(repl)
            if out != m.group(0):
                rewrites.append(Rewrite(field, m.group(0), out))
            return out

        text = pattern.sub(record, text)
    return text


def scrub_obj(obj: Any, ids: LocalIdentifiers) -> tuple[Any, list[Rewrite]]:
    """scrub_local_identifiers over every string in a JSON-like structure (dicts, lists,
    strings). Returns (new object, rewrites); the input is not mutated. Field names in the
    rewrites follow sync_cmd's convention: "user_text", "tool_summaries[2]", ..."""
    rewrites: list[Rewrite] = []

    def walk(o: Any, field: str) -> Any:
        if isinstance(o, str):
            return scrub_local_identifiers(o, ids, rewrites, field)
        if isinstance(o, list):
            return [walk(v, f"{field}[{i}]") for i, v in enumerate(o)]
        if isinstance(o, tuple):
            return tuple(walk(v, f"{field}[{i}]") for i, v in enumerate(o))
        if isinstance(o, dict):
            return {k: walk(v, k if isinstance(k, str) else field) for k, v in o.items()}
        return o

    return walk(obj, ""), rewrites
