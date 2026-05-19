"""CI gate for ``README.md`` and ``AGENTS.md``.

The validation contract assertion ``cross.readme-matches`` requires that
the docs only describe commands and config keys that actually exist in
the committed code. This file enforces both halves:

1. Every ``--flag-name`` referenced in a ``run.py`` command line in
   ``README.md`` must appear verbatim in ``src/cli.py`` (proving the
   flag is wired into the bot's argparse surface, not just mypy / ruff /
   pytest).
2. Every dotted YAML config key referenced inside backticks in
   ``README.md`` or ``AGENTS.md`` (e.g. ``checkout.auto_purchase``)
   must resolve to a real field on :class:`BotConfig` or one of the
   nested dataclasses it composes.

Running ``pytest tests/test_readme.py`` is the externally-checkable
verification the F6.2 feature ships.
"""

from __future__ import annotations

import re
import typing
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from src.utils.config_loader import BotConfig, EventConfig

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
README_PATH: Path = REPO_ROOT / "README.md"
AGENTS_PATH: Path = REPO_ROOT / "AGENTS.md"
CLI_PATH: Path = REPO_ROOT / "src" / "cli.py"

# --- CLI flag extraction --------------------------------------------------

#: Matches ``--flag-name`` (no value); used to enumerate flags referenced
#: in command-line examples. We deliberately require two leading dashes
#: so short options like ``-n`` and ``-c`` are skipped (they are aliases
#: that either appear in ``run.py`` lines with their long form alongside
#: or belong to ``pytest`` / ``ruff``).
_FLAG_RE: re.Pattern[str] = re.compile(r"--[a-z][a-z0-9-]*")


def _readme_run_py_flags() -> set[str]:
    """Return every ``--flag`` that appears on a ``run.py`` command line.

    Scanning the README line-by-line and keeping only lines that mention
    ``run.py`` is the cleanest way to scope flag extraction to *this*
    project's CLI — the file also documents commands like
    ``ruff check --fix`` and ``pytest -n 4`` which use their own flags.
    """
    flags: set[str] = set()
    text = README_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "run.py" not in line:
            continue
        for match in _FLAG_RE.findall(line):
            flags.add(match)
    return flags


# --- Config-key extraction -------------------------------------------------

#: Matches a dotted lowercase identifier path inside backticks. We
#: require ≥1 dot so single-segment names like ``checkout`` aren't
#: picked up (those are usually section headers, not config keys), and
#: we anchor on backticks to avoid prose ambiguity (``e.g.`` etc.).
_BACKTICK_PATH_RE: re.Pattern[str] = re.compile(
    r"`(?P<path>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)`"
)

# Top-level fields of :class:`BotConfig` that the README/AGENTS.md may
# reference. ``event`` is an alias for ``events[0]`` (via the property
# on :class:`BotConfig`), so it walks through :class:`EventConfig` even
# though the dataclass field is named ``events``.
_TOPLEVEL_ALIASES: dict[str, type] = {"event": EventConfig}


def _toplevel_config_names() -> set[str]:
    """Return the set of valid top-level config keys.

    The set is dynamically derived from :class:`BotConfig`'s declared
    fields plus the ``event`` alias property so we don't drift if the
    dataclass gains a new top-level group.
    """
    return {f.name for f in fields(BotConfig)} | set(_TOPLEVEL_ALIASES)


#: File-extension suffixes that mark a backticked dotted token as a
#: filename (e.g. ``accounts.yaml``, ``checkout.py``) rather than a
#: config key. These are stripped from the cross-check so prose like
#: "copy `accounts.yaml.example` to `accounts.yaml`" does not produce
#: false positives — the value happens to start with the top-level
#: ``accounts`` field, but it's plainly a path.
_FILENAME_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".md",
    ".txt",
    ".sh",
    ".html",
    ".example",
)

#: Config keys parsed at runtime that don't live on :class:`BotConfig`
#: (or any of its nested dataclasses) but are nevertheless real YAML
#: keys the documentation may reference. Currently this is the routing
#: surface owned by ``MultiplexNotifier.from_config`` in
#: ``src/notifiers/multiplex.py``, which reads its own ``channels`` and
#: ``routing`` sub-blocks directly from the merged dict.
_RUNTIME_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "notifications.channels",
        "notifications.routing",
    }
)


def _config_paths_in(text: str) -> set[str]:
    """Return every dotted config-key path inside backticks in ``text``.

    Only paths whose first segment is a top-level field on
    :class:`BotConfig` (or the ``event`` alias) are returned, and any
    path whose final segment looks like a filename (``.py``, ``.yaml``,
    …) is excluded — those are file references, not config keys.
    """
    toplevels = _toplevel_config_names()
    paths: set[str] = set()
    for match in _BACKTICK_PATH_RE.finditer(text):
        path = match.group("path")
        if path.split(".", 1)[0] not in toplevels:
            continue
        if path.endswith(_FILENAME_EXTENSIONS):
            continue
        paths.add(path)
    return paths


def _unwrap_type(annotation: object) -> type | None:
    """Reduce a type annotation to a concrete class for further walking.

    Strips ``Optional`` / ``Union[None, T]`` and unwraps ``list[T]`` /
    ``tuple[T, ...]`` to ``T``. Returns ``None`` when the annotation
    isn't a class we can walk into (e.g. a primitive).
    """
    if isinstance(annotation, type):
        return annotation
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if not non_none:
            return None
        return _unwrap_type(non_none[0])
    if origin in (list, tuple, set, frozenset):
        if not args:
            return None
        return _unwrap_type(args[0])
    if isinstance(origin, type):
        return origin
    return None


def _walk(cls: type, parts: list[str]) -> bool:
    """Return ``True`` when ``parts`` resolves to a field on ``cls``.

    Recursively descends into nested dataclasses. Stops successfully as
    soon as ``parts`` is exhausted; bails out at the first segment that
    isn't a field of the current dataclass.
    """
    if not parts:
        return True
    if not is_dataclass(cls):
        return False
    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # noqa: BLE001 - defensive; we want the test to fail loud
        hints = {}
    field_names = {f.name for f in fields(cls)}
    head = parts[0]
    if head not in field_names:
        return False
    annotation = hints.get(head)
    if annotation is None:
        # If we can't introspect the annotation, the field exists but we
        # cannot descend further. That's only OK when ``parts`` is the
        # final segment.
        return len(parts) == 1
    next_cls = _unwrap_type(annotation)
    if next_cls is None:
        return len(parts) == 1
    if is_dataclass(next_cls):
        return _walk(next_cls, parts[1:])
    return len(parts) == 1


def _resolve_config_path(path: str) -> bool:
    if path in _RUNTIME_EXTRA_KEYS:
        return True
    parts = path.split(".")
    head = parts[0]
    if head in _TOPLEVEL_ALIASES:
        return _walk(_TOPLEVEL_ALIASES[head], parts[1:])
    return _walk(BotConfig, parts)


# --- Tests -----------------------------------------------------------------


def test_readme_exists_and_is_non_trivial() -> None:
    assert README_PATH.is_file(), f"Missing README at {README_PATH}"
    assert README_PATH.stat().st_size > 1024, (
        f"README at {README_PATH} is suspiciously small ({README_PATH.stat().st_size} bytes)"
    )


def test_agents_exists_and_is_non_trivial() -> None:
    assert AGENTS_PATH.is_file(), f"Missing AGENTS.md at {AGENTS_PATH}"
    assert AGENTS_PATH.stat().st_size > 1024, (
        f"AGENTS.md at {AGENTS_PATH} is suspiciously small ({AGENTS_PATH.stat().st_size} bytes)"
    )


def test_every_readme_run_py_flag_is_in_cli_module() -> None:
    """Every ``--flag`` shown on a ``run.py`` line resolves in ``src/cli.py``.

    The check is a literal ``in`` against the CLI source — the same
    grep gate the validation contract specifies for
    ``cross.readme-matches``. Any documented flag that has no
    declaration in :mod:`src.cli` fails this test.
    """
    flags = _readme_run_py_flags()
    assert flags, (
        "README does not document any run.py flags; "
        "expected at least --dry-run, --config, --headless, etc."
    )
    cli_source = CLI_PATH.read_text(encoding="utf-8")
    missing = sorted(flag for flag in flags if f'"{flag}"' not in cli_source)
    assert not missing, f"README documents flags that do not appear in {CLI_PATH}: {missing}"


@pytest.mark.parametrize("doc_path", [README_PATH, AGENTS_PATH])
def test_every_backticked_config_path_resolves_in_botconfig(doc_path: Path) -> None:
    """Every backticked dotted path resolves to a real BotConfig field."""
    text = doc_path.read_text(encoding="utf-8")
    paths = _config_paths_in(text)
    missing = sorted(path for path in paths if not _resolve_config_path(path))
    assert not missing, (
        f"{doc_path} references config keys that do not exist on BotConfig: {missing}"
    )


def test_readme_documents_core_subsystems() -> None:
    """README mentions every subsystem doc shipped under ``docs/``.

    The F6.2 feature description requires "one section per subsystem
    linking to docs/"; assert each docs/<name>.md is referenced in the
    README so the README acts as the canonical index.
    """
    required_docs = (
        "architecture.md",
        "strategies.md",
        "notifiers.md",
        "vendors.md",
        "hooks.md",
        "humanize.md",
        "observability.md",
        "parallel.md",
    )
    text = README_PATH.read_text(encoding="utf-8")
    missing = [name for name in required_docs if f"docs/{name}" not in text]
    assert not missing, (
        f"README does not link to docs/{{{', '.join(missing)}}}; "
        "every subsystem doc must be referenced from the README index."
    )


def test_agents_documents_pytest_xdist_commands() -> None:
    """AGENTS.md teaches workers the parallel-pytest commands.

    The F6.2 feature explicitly requires AGENTS.md to mention
    ``pytest -n 8`` (unit) and ``pytest -n 4 tests/integration`` (real-
    Chromium safe parallelism).
    """
    text = AGENTS_PATH.read_text(encoding="utf-8")
    assert "pytest -n 8" in text, "AGENTS.md must document `pytest -n 8` for the parallel unit run."
    assert "pytest -n 4" in text, (
        "AGENTS.md must document `pytest -n 4` for the integration parallel run."
    )


def test_agents_documents_lint_and_type_commands() -> None:
    """AGENTS.md teaches workers the ruff/mypy invocations."""
    text = AGENTS_PATH.read_text(encoding="utf-8")
    assert "ruff check" in text, "AGENTS.md must document `ruff check`."
    assert "mypy" in text, "AGENTS.md must document the mypy command."


def test_agents_documents_extension_recipes() -> None:
    """AGENTS.md walks contributors through the extension surfaces.

    F6.2 requires recipes for adding strategies, notifiers, hooks, and
    vendors. We assert the recipe headings (or equivalent prose
    anchors) exist by looking for the literal markers ``Adding a
    strategy``, ``Adding a notifier``, ``Adding a hook``, and
    ``Adding a vendor`` (case-insensitive).
    """
    text = AGENTS_PATH.read_text(encoding="utf-8").lower()
    for topic in ("strategy", "notifier", "hook", "vendor"):
        anchor = f"adding a {topic}"
        assert anchor in text, f"AGENTS.md is missing the `{anchor.title()}` recipe section."
