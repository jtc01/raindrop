"""ingest_exchanges accounting: which vector-write failures still count an
exchange as accepted (deterministic, client must move on) vs. not accepted
(transient, client retries). All AWS calls are mocked."""

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
