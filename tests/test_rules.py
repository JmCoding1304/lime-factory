"""`lime.rules` — the AGENTS.md/CLAUDE.md rules block plus the ADR filename index."""

from pathlib import Path

from lime import rules


def test_agents_wins_over_claude(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents body\n")
    (tmp_path / "CLAUDE.md").write_text("claude body\n")
    block = rules.rules_block(tmp_path)
    assert block.startswith("agents body\n")
    assert "claude body" not in block


def test_empty_agents_does_not_fall_back_to_claude(tmp_path):
    (tmp_path / "AGENTS.md").write_text("")
    (tmp_path / "CLAUDE.md").write_text("claude body\n")
    block = rules.rules_block(tmp_path)
    assert "claude body" not in block
    assert block == "\n\n## ADRs\n"


def test_claude_fallback_when_there_is_no_agents(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude body\n")
    assert rules.rules_block(tmp_path) == "claude body\n\n\n## ADRs\n"


def test_no_rules_file_returns_exactly_empty_even_with_adrs(tmp_path):
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-x.md").write_text("body\n")
    assert rules.rules_block(tmp_path) == ""


def test_missing_adr_directory_still_appends_the_heading(tmp_path):
    (tmp_path / "AGENTS.md").write_text("rules\n")
    assert rules.rules_block(tmp_path) == "rules\n\n\n## ADRs\n"


def test_complete_formatting_for_a_small_fixture(tmp_path):
    """Text verbatim, blank line, `## ADRs`, then the sorted filename index."""
    (tmp_path / "AGENTS.md").write_text("line1\nline2\n")
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    for name in ("0002-file-name.md", "0001-first.md", "0001-second.md"):
        (adr / name).write_text("body\n")
    assert rules.rules_block(tmp_path) == (
        "line1\nline2\n"
        "\n\n## ADRs\n"
        "0001 first\n0001 second\n0002 file-name\n"
    )


def test_adr_index_is_sorted_and_skips_nested_and_non_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("r\n")
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0003-third.md").write_text("x")
    (adr / "0001-first.md").write_text("x")
    (adr / "notes.txt").write_text("x")
    nested = adr / "sub"
    nested.mkdir()
    (nested / "0009-nested.md").write_text("x")
    block = rules.rules_block(tmp_path)
    assert "0001 first" in block
    assert "0003 third" in block
    assert "0009 nested" not in block
    assert "notes" not in block.split("## ADRs", 1)[1]
    assert block.index("0001 first") < block.index("0003 third")


def test_entry_replaces_only_the_first_hyphen(tmp_path):
    (tmp_path / "AGENTS.md").write_text("r\n")
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-multi-part-slug.md").write_text("x")
    assert "0001 multi-part-slug" in rules.rules_block(tmp_path)


def test_adr_body_is_never_read_into_the_block(tmp_path):
    (tmp_path / "AGENTS.md").write_text("r\n")
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-slug.md").write_text("SECRET ADR BODY SENTINEL\n")
    block = rules.rules_block(tmp_path)
    assert "SECRET ADR BODY SENTINEL" not in block
    assert "0001 slug" in block


def test_adr_bodies_are_not_opened(tmp_path, monkeypatch):
    """A read spy proves the ADR body is never opened — no permission tricks needed."""
    (tmp_path / "AGENTS.md").write_text("r\n")
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    target = adr / "0001-slug.md"
    target.write_text("SECRET\n")
    real_read_text = Path.read_text
    seen: list[Path] = []

    def spy(self, *a, **k):
        seen.append(Path(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    rules.rules_block(tmp_path)
    assert (tmp_path / "AGENTS.md") in seen
    assert target not in seen


def test_substantial_rules_and_large_index_keep_whitespace_and_sentinels(tmp_path):
    """>60 lines is preserved verbatim (the ceiling is a `lime check` rule, not a truncation),
    and leading/trailing whitespace and the index tail survive."""
    rules_text = "  leading\n" + "\n".join(f"line {i}" for i in range(80)) + "\ntrailing  \n"
    (tmp_path / "AGENTS.md").write_text(rules_text)
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    for i in range(40):
        (adr / f"{i:04d}-slug-{i}.md").write_text("x")
    block = rules.rules_block(tmp_path)
    assert rules_text in block
    assert block.startswith("  leading\n")
    assert "line 79" in block
    assert block.endswith("slug-39\n")
