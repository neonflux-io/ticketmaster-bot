"""CI gate for the ``docs/*.md`` set shipped by F6.1.

Every file listed in :data:`REQUIRED_DOCS` must:

1. Exist under ``docs/`` at the repo root.
2. Carry more than ``1024`` bytes of content (the "non-trivial content"
   bar from validation contract assertion ``cross.docs-shipped``).
3. Reference at least one real path inside ``src/`` or ``config/`` that
   currently exists in the repo. The reference is detected by a
   permissive regex that matches the form ``src/...`` or ``config/...``
   inside the document (with or without surrounding backticks); each
   match is then resolved against the repo root.

This file is the "CI check" the feature description requires: a single
pytest test per doc that greps for source-file references and confirms
each one resolves. Running ``pytest tests/test_docs.py`` is the
externally-checkable verification.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Repo root resolved from this file's location (``tests/test_docs.py`` →
#: repo root is two parents up).
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Docs directory.
DOCS_DIR: Path = REPO_ROOT / "docs"

#: The eight documents the F6.1 feature ships.
REQUIRED_DOCS: tuple[str, ...] = (
    "architecture.md",
    "strategies.md",
    "notifiers.md",
    "vendors.md",
    "hooks.md",
    "humanize.md",
    "observability.md",
    "parallel.md",
)

#: Minimum byte count (>1 KB per the validation contract).
MIN_DOC_BYTES: int = 1024

#: Matches ``src/...`` and ``config/...`` paths inside markdown text.
#: Captures a leading path segment (``src/`` or ``config/``) followed by
#: a non-greedy run of path-safe characters, terminated by whitespace,
#: a closing backtick, a closing paren, a comma, a semicolon, or end of
#: input. Trailing punctuation like a period is stripped at match-time
#: by the resolver below so prose like "see src/cli.py." still works.
_PATH_RE: re.Pattern[str] = re.compile(r"(?P<path>(?:src|config)/[A-Za-z0-9_./\-]+)")


def _extracted_paths(text: str) -> list[str]:
    """Return every ``src/...`` / ``config/...`` path mentioned in ``text``.

    Trailing periods, commas, semicolons, and closing parens are
    stripped because they are virtually always prose punctuation rather
    than path components.
    """
    out: list[str] = []
    for match in _PATH_RE.finditer(text):
        raw = match.group("path")
        cleaned = raw.rstrip(".,;:)`>")
        if cleaned:
            out.append(cleaned)
    return out


@pytest.mark.parametrize("doc_name", REQUIRED_DOCS)
def test_doc_exists_and_is_non_trivial(doc_name: str) -> None:
    """Every required doc exists and has more than 1 KB of content."""
    doc_path = DOCS_DIR / doc_name
    assert doc_path.is_file(), f"Missing doc: {doc_path}"
    size = doc_path.stat().st_size
    assert size > MIN_DOC_BYTES, (
        f"Doc {doc_path} is too small ({size} bytes); "
        f"validation contract requires >{MIN_DOC_BYTES} bytes."
    )


@pytest.mark.parametrize("doc_name", REQUIRED_DOCS)
def test_doc_references_real_source_file(doc_name: str) -> None:
    """Every required doc references at least one path under src/ or config/.

    The validation contract assertion ``cross.docs-shipped`` requires
    that the referenced file actually exists in the repo. This test
    enforces both halves: there must be ≥1 reference, and every
    reference must resolve to a real file or directory.
    """
    doc_path = DOCS_DIR / doc_name
    text = doc_path.read_text(encoding="utf-8")
    paths = _extracted_paths(text)
    assert paths, (
        f"Doc {doc_path} does not reference any src/ or config/ paths; "
        "the CI check requires at least one real source-file reference."
    )

    missing: list[str] = []
    for rel_path in paths:
        target = REPO_ROOT / rel_path
        if not target.exists():
            missing.append(rel_path)

    assert not missing, f"Doc {doc_path} references paths that do not exist in the repo: {missing}"


def test_docs_directory_contains_only_expected_files() -> None:
    """The ``docs/`` directory should contain exactly the F6.1 set.

    Extra files are not a hard error, but unexpected extensions or
    stray temp files (``.swp``, ``.bak``, ``.tmp``) suggest editor
    leftovers that should not be committed. We allow ``.md`` files
    only — anything else fails this check loudly.
    """
    if not DOCS_DIR.is_dir():
        pytest.fail(f"docs directory does not exist: {DOCS_DIR}")
    unexpected = [
        entry.name
        for entry in DOCS_DIR.iterdir()
        if entry.is_file() and entry.suffix.lower() != ".md"
    ]
    assert not unexpected, f"Unexpected non-markdown files in {DOCS_DIR}: {unexpected}"
