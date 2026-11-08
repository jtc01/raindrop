import unittest

from raindrop.redact import REDACTED, redact, redact_obj


class RedactTests(unittest.TestCase):
    def assertGone(self, secret, text, **kw):
        out = redact(text, **kw)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED", out)
        return out

    def test_provider_tokens(self):
        cases = [
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "a" * 36,
            "github_pat_" + "A1b2" * 12,
            "xoxb-1234567890-abcdefghij",
            "sk_live_" + "a1B2c3D4" * 3,
            "AIza" + "x" * 35,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123_-sig",
        ]
        for s in cases:
            with self.subTest(s=s):
                self.assertGone(s, f"here is my token {s} ok")

    def test_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----"
        out = self.assertGone("MIIabc", f"key:\n{pem}\nafter")
        self.assertIn("after", out)

    def test_key_value_pairs(self):
        for text, secret in [
            ('OPENAI_API_KEY="hunter2hunter2"', "hunter2hunter2"),
            ("db_password = s3cretPass!", "s3cretPass!"),
            ('{"client_secret": "abcd1234efgh"}', "abcd1234efgh"),
            ("Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
            ("authorization = Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
            ("export STRIPE_TOKEN=zzzzzzzzzzzz", "zzzzzzzzzzzz"),
        ]:
            with self.subTest(text=text):
                out = self.assertGone(secret, text)
        # key name preserved for context
        self.assertIn("db_password", redact("db_password = s3cretPass!"))

    def test_real_values_near_those_placeholders_still_redacted(self):
        self.assertNotIn("hunter2xyz", redact("secret=hunter2xyz"))
        self.assertNotIn("correcthorse", redact("password = correcthorse"))
        self.assertNotIn("S3cr3tPw", redact("postgres://user:S3cr3tPw@host/db"))

    def test_url_credentials(self):
        out = redact("postgres://admin:p4ssw0rd@db.internal:5432/app")
        self.assertNotIn("p4ssw0rd", out)
        self.assertIn("admin", out)
        self.assertIn("db.internal", out)

    def test_known_secrets(self):
        key = "Zm9vYmFyYmF6cXV4MTIzNDU2"
        out = redact(f"the key is {key}, really {key}", known_secrets=[key])
        self.assertNotIn(key, out)
        # short known secrets are ignored
        self.assertEqual(redact("hello abc world", known_secrets=["abc"]), "hello abc world")

    def test_no_false_positives_on_code_and_prose(self):
        for text in [
            "token = get_token()",
            "password: str",
            "api_key = os.environ['X']",
            "self.token = config.api_key",
            "key = ${API_KEY}",
            "Fix the auth middleware so it returns 401",
            "def authenticate(token: str) -> bool:",
            "Authorization: Bearer <key> and X-Session-Id",
            "git config credential.https://github.com.username NAME",
            "clean = redact(text, known_secrets=secrets)",
            "token = token",
            "postgres://user:pass@host/db",
            "postgres://admin:password@localhost:5432/app",
            "git commit -m 'fix bug' && ls src/components",
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_idempotent(self):
        once = redact('api_key="abcdefghijklmnop" and ghp_' + "a" * 36)
        self.assertEqual(redact(once), once)

    def test_session_id_is_not_redacted(self):
        sid = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b"
        wire = {"id": f"{sid}:3", "session_id": sid, "idx": 3, "user_text": f"session_id: {sid}"}
        self.assertEqual(redact_obj(wire), wire)

    def test_redact_obj(self):
        ex = {
            "prompt": "use sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
            "meta": {"api_key": "plainvalue123", "n": 3},
            "files": ["a.py", "b.py"],
        }
        out = redact_obj(ex)
        self.assertEqual(out["meta"]["api_key"], REDACTED)
        self.assertEqual(out["meta"]["n"], 3)
        self.assertEqual(out["files"], ["a.py", "b.py"])
        self.assertNotIn("sk-ant", out["prompt"])
        self.assertIn("sk-ant", ex["prompt"])  # input not mutated


class EntropyAndEnvTests(unittest.TestCase):
    def test_high_entropy_unlabeled(self):
        s = "q8Zk3Lp0Xv7TnR2bYw9Fa4HcJd6MsEu1"
        out = redact(f"paste this: {s} thanks")
        self.assertNotIn(s, out)
        self.assertIn("high-entropy", out)

    def test_entropy_ignores_hashes_paths_identifiers(self):
        for text in [
            "commit 8ffce97a1b2c3d4e5f60718293a4b5c6d7e8f901",
            "sha256 " + "ab12" * 16,
            "src/components/very_long_directory_name_here/file.py",
            "test_no_false_positives_on_code_and_prose_2",
            "a" * 40,
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    BLOB = __import__("base64").urlsafe_b64encode(__import__("hashlib").sha512(b"x").digest()).decode().rstrip("=")

    def test_random_blob_is_redacted_but_benign_shapes_are_not(self):
        b = self.BLOB
        self.assertIn("high-entropy", redact(f"token-ish {b} end"))  # control
        for text in [
            "id 019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b done",
            f'"integrity": "sha512-{b}=="',
            f"![img](data:image/png;base64,{b})",
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_entropy_log_is_opt_in_and_never_contains_the_string(self):
        import os, tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "redact.log"
            with mock.patch.dict(os.environ, {"RAINDROP_HOME": d}, clear=False):
                os.environ.pop("RAINDROP_REDACT_LOG", None)
                redact(f"x {self.BLOB} y")
                self.assertFalse(log.exists())  # off by default
                os.environ["RAINDROP_REDACT_LOG"] = "1"
                redact(f"x {self.BLOB} y")
            text = log.read_text(encoding="utf-8")
            self.assertIn("high-entropy", text)
            self.assertIn(f"len={len(self.BLOB)}", text)
            self.assertNotIn(self.BLOB[:12], text)

    def test_load_env_secrets(self):
        import tempfile
        from pathlib import Path
        from raindrop.redact import load_env_secrets

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_text(
                "# comment\nNODE_ENV=production\nDB_PASSWORD='correct-horse-battery'\n"
                "export STRIPE_SECRET=short\nMY_API_KEY=abcdefgh12345678\n"
            )
            secrets = load_env_secrets(d)
            self.assertEqual(sorted(secrets), ["abcdefgh12345678", "correct-horse-battery"])
            out = redact("pw is correct-horse-battery in production", known_secrets=secrets)
            self.assertNotIn("correct-horse-battery", out)
            self.assertIn("production", out)
        self.assertEqual(load_env_secrets("does/not/exist"), [])


class ToolSummaryFalsePositiveTests(unittest.TestCase):
    """Found by scanning real transcripts: harmless text in tool summaries was being redacted."""

    BS = chr(92)  # a backslash, without escaping trouble

    def test_auth_inside_another_word_is_not_a_secret_name(self):
        for text in [
            "git commit -m x && TRAILER='Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>'",
            "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
            "author = someone_long_name_here",
            "authored_by: Someone Long Name",
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_auth_as_a_word_is_still_a_secret_name(self):
        for text, secret in [
            ("Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
            ("authorization = Basic dXNlcjpwYXNzd29yZDEyMzQ=", "dXNlcjpwYXNzd29yZDEyMzQ"),
            ("auth_key = s3cr3tvalue99", "s3cr3tvalue99"),
            ("authKey: 'hunter2hunter2'", "hunter2hunter2"),
            ("OAUTH=Zm9vYmFyYmF6cXV4MTIzNDU2", "Zm9vYmFyYmF6cXV4MTIzNDU2"),
            ("x-auth-token: tok_live_abcdefgh12345678", "tok_live_abcdefgh12345678"),
        ]:
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))

    def test_a_long_folder_name_in_a_path_is_not_a_high_entropy_secret(self):
        folder = "D--Documents-iMazing-Backups-00008150-001C34AA1100401C"
        bs = self.BS
        for text in [
            "Read C:" + bs + "Users" + bs + "me" + bs + "claude" + bs + folder + bs + "x.output",   # Windows
            "Read C:/Users/me/claude/" + folder + "/x.output",                                       # forward slashes
        ]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_a_pasted_random_blob_is_still_caught_even_next_to_a_path(self):
        blob = "q8Zk3Lp0Xv7TnR2bYw9Fa4HcJd6MsEu1"
        self.assertNotIn(blob, redact("see " + blob + " pasted"))


if __name__ == "__main__":
    unittest.main()
