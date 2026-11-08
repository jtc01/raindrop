"""The text embedded for one exchange is capped at about 500 tokens (2000 characters)."""

import itertools

import embed
from embed import build_exchange_embedding_text as build

CAP = embed.EMBED_TEXT_MAX_TOKENS * embed.EMBED_CHARS_PER_TOKEN


def test_default_cap_is_about_500_tokens():
    assert embed.EMBED_TEXT_MAX_TOKENS == 500 and CAP == 2000


def test_short_exchange_is_embedded_whole_in_the_usual_layout():
    assert build("how do I x", "like this", ["a.py", "b.py"]) == "how do I x\n\nlike this\n\nFiles: a.py, b.py"
    assert build("  q  ", "  a  ") == "q\n\na"  # no files: no files line


def test_a_long_reply_is_cut_and_the_prompt_survives_whole():
    out = build("fix the login bug", "word " * 5000, ["src/login.py"])
    assert len(out) <= CAP
    assert out.startswith("fix the login bug\n\nword")
    assert "…" in out and out.endswith("Files: src/login.py")


def test_when_both_are_long_the_prompt_keeps_at_least_forty_percent_of_the_room():
    out = build("P" * 9000, "R" * 9000)
    assert len(out) <= CAP
    prompt, reply = out.split("\n\n")
    assert len(prompt) >= 0.4 * (CAP - 2) - 1 and len(reply) >= len(prompt) * 0.5  # neither is starved
    assert prompt.endswith("…") and reply.endswith("…")


def test_a_long_prompt_does_not_squeeze_out_a_short_reply():
    out = build("P" * 9000, "short reply")
    assert out.endswith("short reply") and len(out) <= CAP


def test_many_files_are_listed_by_whole_paths_and_counted_not_cut_mid_path():
    files = [f"src/pkg/module_{i}/handler.py" for i in range(200)]
    out = build("q", "a", files)
    line = out.split("\n\n")[-1]
    assert len(line) <= CAP * 0.2
    assert line.startswith("Files: src/pkg/module_0/handler.py, ") and ", +" in line and line.endswith(" more")
    shown = [p.strip() for p in line[len("Files: "):].rsplit(", +", 1)[0].split(",")]
    assert all(p.endswith("/handler.py") for p in shown)  # every listed path is whole
    n_more = int(line.rsplit("+", 1)[1].split()[0])
    assert len(shown) + n_more == 200  # nothing silently lost


def test_one_enormous_path_is_clipped_not_dropped():
    out = build("q", "a", ["x/" + "y" * 5000])
    assert len(out) <= CAP and out.split("\n\n")[-1].startswith("Files: x/yyy")


def test_nothing_to_embed_stays_empty_so_ingestion_can_skip_it():
    assert build("", "   ", []) == "" and build(" ", "", [""]) == ""


def test_the_cap_holds_for_a_sweep_of_sizes():
    sizes = [0, 1, 50, 800, 1500, 1999, 2000, 2001, 5000, 40000]
    for u, a, nfiles in itertools.product(sizes, sizes, [0, 1, 30]):
        out = build("u" * u, "a" * a, [f"dir/file_{i}.py" for i in range(nfiles)])
        assert len(out) <= CAP, (u, a, nfiles, len(out))


def test_the_cap_can_be_tuned(monkeypatch):
    monkeypatch.setattr(embed, "EMBED_TEXT_MAX_TOKENS", 50)
    assert len(build("p" * 1000, "r" * 1000)) <= 50 * embed.EMBED_CHARS_PER_TOKEN
