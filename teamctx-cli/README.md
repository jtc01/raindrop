# teamctx secret redaction

`redact.py` scrubs secrets (API keys, tokens, passwords) from text **locally**, before anything is
uploaded to the cloud. It is meant to run inside `teamctx sync` (the Stop-hook auto-sync), right
before the POST to `push_messages`.

Pure Python standard library, no dependencies.

## How it works

`redact()` applies four layers, in order:

| # | Layer | What it catches | Example result |
|---|-------|-----------------|----------------|
| 1 | **Known values** | Exact secrets you pass in (e.g. the teamctx key, values from `.env`) | `[REDACTED]` |
| 2 | **Known formats** | Anthropic, OpenAI, AWS, GitHub, GitLab, Slack, Stripe, Google, npm, SendGrid keys; JWTs; PEM private key blocks; passwords inside URLs | `[REDACTED:anthropic-key]`, `postgres://admin:[REDACTED]@host` |
| 3 | **Key-value pairs** | Values after names like `api_key`, `secret`, `token`, `password`, `Authorization: Bearer ...`, `.env` lines | `db_password = [REDACTED]` |
| 4 | **High entropy** | Long (32+ char) random-looking strings with no label | `[REDACTED:high-entropy]` |

(Layers 2 and 3 run in the order formats, then key-value, so labelled tokens keep their specific label.)

Design notes:

- The key *name* is kept (`db_password = [REDACTED]`) so the redacted text stays readable.
- Code is left alone: `token = get_token()`, `password: str`, `api_key = os.environ['X']`,
  `${API_KEY}` are not redacted.
- Git SHAs, other hex digests, file paths, and long identifiers are not treated as high-entropy secrets.
- Redacting already-redacted text changes nothing (idempotent).
- Known values shorter than 8 characters are ignored, so short strings can't shred normal text.

## Usage

```python
from redact import redact, redact_obj, load_env_secrets

# Secrets we know exactly: the teamctx key from ~/.teamctx/config.json,
# plus secret-looking values from the project's .env files.
known = [cfg["key"]] + load_env_secrets(project_root)

# A plain string
clean = redact(text, known_secrets=known)

# A whole parsed exchange (dicts / lists / strings). Returns a new object;
# the original is not modified.
clean_exchange = redact_obj(exchange, known_secrets=known)
```

### In `teamctx sync`

Call it after parsing the transcript into exchanges and **before** the POST to `push_messages`:

```python
exchanges = parse_exchanges(new_content)          # adapter's parser
known = [cfg["key"]] + load_env_secrets(project_root)
exchanges = [redact_obj(e, known_secrets=known) for e in exchanges]
push_messages(exchanges)                          # only now does data leave the machine
```

Redaction only affects what is uploaded; the local transcript is never modified.

### API

- `redact(text, known_secrets=()) -> str`
- `redact_obj(obj, known_secrets=()) -> same shape as obj`. Redacts every string in nested
  dicts/lists/tuples. If a dict key itself looks secret (e.g. `"api_key"`) and its value is a
  string, the whole value is replaced.
- `load_env_secrets(path) -> list[str]`. Accepts a `.env` file or a directory (reads every
  `.env*` file in it). Returns only values of variables whose *name* looks secret
  (`*_API_KEY`, `*_TOKEN`, `PASSWORD`, ...), so `NODE_ENV=production` is not scrubbed.
  Missing or unreadable files return `[]`.
- Tuning constants: `ENTROPY_THRESHOLD` (default `4.2` bits/char), `MIN_KNOWN_SECRET_LEN` (default `8`).

## Limitations

- Pattern-based: a secret with no recognizable format, no telling variable name, and low
  entropy will not be caught.
- The entropy layer can occasionally redact a random-looking non-secret ID. Raise
  `ENTROPY_THRESHOLD` if it is too noisy.

## Tests

From this directory:

```bash
python -m unittest test_redact -v
```
