# Repository Guidelines

A Playwright-based Python bot that automates Ticketmaster ticket purchasing. This document gives contributors and AI assistants the conventions and commands needed to work productively in this repo.

## Project Structure & Module Organization

```
ticketmaster-bot/
├── run.py                   # Top-level CLI entry point
├── src/
│   ├── main.py              # Argument parsing + bootstrapping
│   ├── bot/                 # Flow modules (auth, navigator, queue, cart, checkout, core)
│   ├── strategies/          # Pluggable ticket-selection strategies
│   └── utils/               # Config loader, logger, retry, notifier
├── config/
│   ├── config.yaml          # Main runtime config (committed)
│   └── accounts.yaml.example # Credential template (real file gitignored)
├── sessions/                # Playwright persistent profiles (gitignored)
├── logs/                    # Runtime logs (gitignored)
└── requirements.txt
```

State flow lives in `src/bot/core.py`; each step (login, queue, select, cart, checkout) is its own module. Add new selection strategies under `src/strategies/` and register them in `factory.py`.

## Build, Test, and Development Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # set up venv
pip install -r requirements.txt                      # install deps
playwright install chromium                          # one-time browser install
python run.py                                        # run with default config
python run.py --config config/config.yaml --headless # custom config, headless
python -m py_compile src/**/*.py                     # quick syntax check
```

## Coding Style & Naming Conventions

- Python 3.10+; 4-space indentation; type hints required on public functions.
- Use `from __future__ import annotations` at the top of new modules.
- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Module names lowercase, single word where possible (`cart.py`, `queue.py`).
- Async functions use the `async def` form; await all I/O.
- Log via `logging.getLogger("ticketmaster-bot")` — never `print()`.
- Selectors live with the module that owns the flow step, not in a shared file.

## Testing Guidelines

- Unit tests live under `tests/` and run via `pytest` + `pytest-asyncio` (asyncio mode = auto, see `pyproject.toml`). Name files `test_<module>.py`.
- Commands:
  1. `python -m py_compile $(find src -name '*.py')` for a syntax check.
  2. `pytest` for unit tests.
  3. `ruff check src tests` and `mypy src` for lint / types.
  4. End-to-end smoke: `python run.py --dry-run` validates config without launching a browser. Then run against a non-critical event with `headless: false` and `auto_purchase: false` and inspect `logs/bot.log`.
- Network/DOM tests use the `FakePage` / `FakeLocator` helpers in `tests/conftest.py`. Don't import real Playwright in unit tests.

## Commit & Pull Request Guidelines

- Commit messages: imperative mood, ≤72 chars, optional scope prefix (`bot:`, `strategies:`, `config:`). Example: `bot: handle queue timeout gracefully`.
- One logical change per commit; rebase before opening a PR.
- PRs must include: summary, config/selector changes called out, manual-test notes, and any updated screenshots of the flow if UI selectors changed.
- Never commit `config/accounts.yaml`, `.env`, `sessions/`, or `logs/`.

## Security & Configuration Tips

- Credentials belong in `.env` or `config/accounts.yaml` (both gitignored); reference env vars in YAML with `${VAR_NAME}`.
- Keep `checkout.auto_purchase: false` until a flow is verified end-to-end.
- Persistent Chromium profiles in `sessions/` contain auth cookies — treat as secrets.
