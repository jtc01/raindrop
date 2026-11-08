"""ingest_exchanges accounting: which vector-write failures still count an
exchange as accepted (deterministic, client must move on) vs. not accepted
(transient, client retries). All AWS calls are mocked."""

import json
import logging
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import messages
import vector_store
from embed import EmbeddingError, EmptyTextError

IDENT = {"team_id": "team_t", "agent_id": "agt_a", "display_name": "Tester"}


def _exchange(**overrides):
    ex = {
        "session_id": "sess",
        "idx": 0,
        "ts": 1700000000,
        "user_text": "how do I do X",
        "assistant_text": "like this",
        "tool_summaries": [],
        "files_touched": [],
    }
    ex.update(overrides)
    return ex


@pytest.fixture
def s3_ok():
    with mock.patch.object(messages._s3, "put_object") as put:
        yield put


def test_empty_text_is_accepted_but_not_indexed(s3_ok, caplog):
    # Real embed_text raises EmptyTextError before calling Bedrock when the text is empty.
    with mock.patch.object(messages, "put_vector") as put_vector, caplog.at_level(logging.WARNING):
        result = messages.ingest_exchanges(IDENT, [_exchange(user_text="", assistant_text="")])
    assert result == {"accepted": 1, "ids": ["sess:0"]}
    put_vector.assert_not_called()
    s3_ok.assert_called_once()
    assert "vector_write_skipped=true reason=empty_text" in caplog.text


def test_empty_text_with_files_touched_still_embeds(s3_ok):
    # files_touched alone is embeddable text, so this must NOT be treated as empty.
    with mock.patch.object(messages, "embed_text", return_value=[0.1]) as embed, \
         mock.patch.object(messages, "put_vector") as put_vector:
        result = messages.ingest_exchanges(
            IDENT, [_exchange(user_text="", assistant_text="", files_touched=["src/app.py"])]
        )
    assert result["accepted"] == 1
    embed.assert_called_once_with("Files: src/app.py")
    put_vector.assert_called_once()


def test_bad_titan_response_is_not_accepted(s3_ok):
    with mock.patch.object(messages, "embed_text", side_effect=EmbeddingError("unexpected Titan response shape")), \
         mock.patch.object(messages, "put_vector") as put_vector:
        result = messages.ingest_exchanges(IDENT, [_exchange()])
    assert result == {"accepted": 0, "ids": []}
    put_vector.assert_not_called()


def test_validation_error_is_accepted(s3_ok, caplog):
    with mock.patch.object(messages, "embed_text", return_value=[0.1]), \
         mock.patch.object(messages, "put_vector", side_effect=vector_store.VectorStoreValidationError("rejected")), \
         caplog.at_level(logging.ERROR):
        result = messages.ingest_exchanges(IDENT, [_exchange()])
    assert result == {"accepted": 1, "ids": ["sess:0"]}
    assert "vector_write_rejected=true" in caplog.text


def test_transient_vector_error_is_not_accepted(s3_ok):
    with mock.patch.object(messages, "embed_text", return_value=[0.1]), \
         mock.patch.object(messages, "put_vector", side_effect=vector_store.VectorStoreError("throttled")):
        result = messages.ingest_exchanges(IDENT, [_exchange()])
    assert result == {"accepted": 0, "ids": []}


def test_s3_failure_skips_exchange():
    with mock.patch.object(messages._s3, "put_object", side_effect=RuntimeError("s3 down")), \
         mock.patch.object(messages, "embed_text") as embed:
        result = messages.ingest_exchanges(IDENT, [_exchange()])
    assert result == {"accepted": 0, "ids": []}
    embed.assert_not_called()


def test_empty_text_error_is_an_embedding_error():
    assert issubclass(EmptyTextError, EmbeddingError)


# --- vector_store: metadata sanitizing + error classification ---

def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "PutVectors")


def test_put_vectors_drops_empty_metadata_values():
    sent = []
    with mock.patch.object(vector_store._s3vectors, "put_vectors", side_effect=lambda **kw: sent.append(kw)):
        vector_store.put_vector(
            "k", [0.1],
            {"team_id": "t", "idx": 0, "flag": False, "files_touched": [], "none": None, "d": {}, "s": ""},
        )
    assert sent[0]["vectors"][0]["metadata"] == {"team_id": "t", "idx": 0, "flag": False}


def test_put_vectors_validation_exception_maps_to_validation_error():
    with mock.patch.object(vector_store._s3vectors, "put_vectors", side_effect=_client_error("ValidationException")):
        with pytest.raises(vector_store.VectorStoreValidationError):
            vector_store.put_vector("k", [0.1], {})


def test_put_vectors_other_client_error_stays_retryable():
    with mock.patch.object(vector_store._s3vectors, "put_vectors", side_effect=_client_error("ThrottlingException")):
        with pytest.raises(vector_store.VectorStoreError) as info:
            vector_store.put_vector("k", [0.1], {})
    assert not isinstance(info.value, vector_store.VectorStoreValidationError)


# --- server-side path scrub (defense in depth behind the client's redaction) ---

import pytest as _pytest  # noqa: E402  (keeps the block self-contained when appended)


@_pytest.mark.parametrize("text, expected", [
    ("Edit /Users/matt/Development/raindrop/api/app.py", "Edit ~/Development/raindrop/api/app.py"),
    ("/home/phil/Development/x.py and /home/phil/.cursor/hooks.json", "~/Development/x.py and ~/.cursor/hooks.json"),
    (r"C:\Users\Matteo\Documents\GitHub\raindrop\api\app.py", r"~/Documents\GitHub\raindrop\api\app.py"),
    ("C:/Users/Matteo/Documents/x.py", "~/Documents/x.py"),
    (r"c:\users\matteo\x.py", "~/x.py"),
    (r"C:\Users\Matt Moyer\x.py", "~/x.py"),
    ("cd /c/Users/Matteo/Documents/GitHub/raindrop && git status", "cd ~/Documents/GitHub/raindrop && git status"),
    ("/USERS/MATT/x", "~/x"),
    ('"/Users/matt/x" [/home/a/b]', '"~/x" [~/b]'),
    ("/home/phil/.claude/projects/-home-phil-Development-raindrop/x.txt",
     "~/.claude/projects/-home-<user>-Development-raindrop/x.txt"),
    (r"C:\Users\Matteo\.claude\projects\-Users-Matteo-Documents-x\y", r"~/.claude\projects\-Users-<user>-Documents-x\y"),
    ("slug-home-made-pie", "slug-home-made-pie"),  # not after a separator
    ("cd /c/Users/Matteo && ls /Users/matt; /home/phil.", "cd ~ && ls ~; ~."),  # bare home dir
    ("the slug is `-Users-matt-Development-raindrop-api`", "the slug is `-Users-<user>-Development-raindrop-api`"),
    # Not home directories: mid-path, relative, already scrubbed, prose.
    ("/opt/home/x/y ./home/x/y ~/Users/x/y Users/home at home", "/opt/home/x/y ./home/x/y ~/Users/x/y Users/home at home"),
    ("", ""),
])
def test_scrub_home_paths(text, expected):
    assert messages.scrub_home_paths(text) == expected


def test_ingest_scrubs_text_fields_before_writing(s3_ok):
    ex = _exchange(
        user_text="fix /Users/matt/Development/raindrop/api/app.py",
        assistant_text="Edited /home/matt/proj/x.py. " + "y" * 300,
        tool_summaries=[r"Edit C:\Users\Matteo\proj\x.py", "Bash: pytest"],
        files_touched=["/home/phil/proj/a.py", "api/app.py"],
    )
    with mock.patch.object(messages, "embed_text", return_value=[0.1]) as embed, \
         mock.patch.object(messages, "put_vector") as put_vector:
        result = messages.ingest_exchanges(IDENT, [ex])
    assert result["accepted"] == 1
    doc = json.loads(s3_ok.call_args.kwargs["Body"])
    assert doc["user_text"] == "fix ~/Development/raindrop/api/app.py"
    assert doc["assistant_text"].startswith("Edited ~/proj/x.py. ")
    assert doc["tool_summaries"] == [r"Edit ~/proj\x.py", "Bash: pytest"]
    assert doc["files_touched"] == ["~/proj/a.py", "api/app.py"]
    assert "/Users/" not in json.dumps(doc) and "/home/" not in json.dumps(doc)
    # The embedding and the vector's preview see the scrubbed text too.
    assert "/Users/" not in embed.call_args.args[0]
    assert put_vector.call_args.kwargs["metadata"]["preview"].startswith("Edited ~/proj/x.py.")
    assert put_vector.call_args.kwargs["metadata"]["files_touched"] == ["~/proj/a.py", "api/app.py"]
    assert ex["user_text"].startswith("fix /Users")  # request body not mutated


def test_fetch_vectors_returns_data_and_metadata():
    resp = {"vectors": [{"key": "k1", "data": {"float32": [0.1, 0.2]}, "metadata": {"preview": "p"}}]}
    with mock.patch.object(vector_store._s3vectors, "get_vectors", return_value=resp) as gv:
        out = vector_store.fetch_vectors(["k1", "k2"])
    assert out == {"k1": ([0.1, 0.2], {"preview": "p"})}
    assert gv.call_args.kwargs["returnData"] is True and gv.call_args.kwargs["returnMetadata"] is True


# ---- embedding runs concurrently --------------------------------------------------------

import threading
import time as _time


def _batch(n):
    return [_exchange(idx=i, user_text=f"question {i}") for i in range(n)]


def test_embeds_overlap_instead_of_running_one_after_another(s3_ok):
    # Two embeds can only get past this barrier if both are running at the same moment.
    # A sequential loop would wait here until the timeout and the test would fail.
    barrier = threading.Barrier(2, timeout=5)

    def embed(text):
        barrier.wait()
        return [0.1]

    with mock.patch.object(messages, "embed_text", side_effect=embed), mock.patch.object(messages, "put_vector"):
        result = messages.ingest_exchanges(IDENT, _batch(2))
    assert result["accepted"] == 2


def test_concurrency_never_exceeds_the_worker_limit(s3_ok, monkeypatch):
    monkeypatch.setattr(messages, "EMBED_MAX_WORKERS", 3)
    lock, running, peak = threading.Lock(), [0], [0]

    def embed(text):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        _time.sleep(0.05)
        with lock:
            running[0] -= 1
        return [0.1]

    with mock.patch.object(messages, "embed_text", side_effect=embed), mock.patch.object(messages, "put_vector"):
        result = messages.ingest_exchanges(IDENT, _batch(12))
    assert result["accepted"] == 12 and 2 <= peak[0] <= 3


def test_one_worker_means_strictly_sequential(s3_ok, monkeypatch):
    monkeypatch.setattr(messages, "EMBED_MAX_WORKERS", 1)
    lock, running, peak = threading.Lock(), [0], [0]

    def embed(text):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        _time.sleep(0.01)
        with lock:
            running[0] -= 1
        return [0.1]

    with mock.patch.object(messages, "embed_text", side_effect=embed), mock.patch.object(messages, "put_vector"):
        messages.ingest_exchanges(IDENT, _batch(5))
    assert peak[0] == 1


def test_ids_come_back_in_request_order_even_when_later_ones_finish_first(s3_ok):
    def embed(text):  # the first exchanges are the slowest
        n = int(text.split("question ")[1].split()[0])
        _time.sleep(0.06 - 0.01 * n)
        return [0.1]

    with mock.patch.object(messages, "embed_text", side_effect=embed), mock.patch.object(messages, "put_vector"):
        result = messages.ingest_exchanges(IDENT, _batch(5))
    assert result["ids"] == [f"sess:{i}" for i in range(5)]


def test_one_failed_exchange_does_not_disturb_the_others(s3_ok):
    def embed(text):
        if "question 2" in text:
            raise EmbeddingError("bad response for exchange 2")
        return [0.1]

    with mock.patch.object(messages, "embed_text", side_effect=embed), mock.patch.object(messages, "put_vector"):
        result = messages.ingest_exchanges(IDENT, _batch(5))
    assert result == {"accepted": 4, "ids": ["sess:0", "sess:1", "sess:3", "sess:4"]}  # 2 is retried by the client


def test_an_unexpected_error_still_propagates_like_before(s3_ok):
    with mock.patch.object(messages, "embed_text", side_effect=RuntimeError("boom")), \
         mock.patch.object(messages, "put_vector"):
        with pytest.raises(RuntimeError, match="boom"):
            messages.ingest_exchanges(IDENT, _batch(4))


def test_only_the_first_fifty_exchanges_of_a_request_are_processed(s3_ok):
    with mock.patch.object(messages, "embed_text", return_value=[0.1]), mock.patch.object(messages, "put_vector"):
        result = messages.ingest_exchanges(IDENT, _batch(60))
    assert result["accepted"] == messages.MAX_EXCHANGES_PER_REQUEST == 50
