# Humanise modules

The `src/humanize/` package owns the anti-detection primitives the
runner uses to make browser activity look less like Playwright and more
like a real user. There are four modules; each one is independently
enabled / tuned through its own block under `timing.humanize` in
`config/config.yaml`.

Stealth (init scripts, fingerprint patches) is a separate concern and
lives in `src/utils/stealth.py`; this document mentions it only where
the two interact.

## Master switch + master gate

`timing.humanize.enabled` in `config/config.yaml` is the global gate.
When `false`, the runner's per-action delay loop is the only
randomisation that runs and every humanise subsystem is bypassed.
When `true`, each subsystem still has its own per-block `enabled` flag
so operators can mix and match (e.g. typing without mouse). The
top-level value is parsed into `HumanizeConfig` by
`src/utils/config_loader.py`.

## `src/humanize/mouse.py` — Bezier mouse moves

`bezier_move(page, start_xy, end_xy, ...)` walks the cursor along a
cubic Bezier (two random control points) instead of teleporting.

### What it does

- Picks two control points within a jitter-proportional bounding box
  around the straight `start → end` segment.
- Emits `step_count` Playwright `page.mouse.move` calls along the
  curve, evaluating
  `_cubic_bezier_point(t, start, cp1, cp2, end)` at
  `t = i / step_count` for `i ∈ [1, step_count]`.
- Sleeps `random.randint(delay_ms_min, delay_ms_max)` milliseconds
  between steps (skipping the sleep after the final move).

### Validation

The `dom.humanize-mouse-bezier` contract assertion requires at least
10 distinct `page.mouse.move` calls per invocation; the defaults make
that floor automatic.

### Config knobs

```yaml
timing:
  humanize:
    enabled: true
    mouse:
      enabled: true
      steps_min: 20      # default DEFAULT_STEPS_MIN
      steps_max: 40      # default DEFAULT_STEPS_MAX
      delay_ms_min: 8    # default DEFAULT_DELAY_MS_MIN
      delay_ms_max: 25   # default DEFAULT_DELAY_MS_MAX
```

A `seed: int | None` kwarg is exposed at the function level for
deterministic tests; the config layer does not currently surface it.
The constants `DEFAULT_STEPS_MIN`, `DEFAULT_STEPS_MAX`,
`DEFAULT_DELAY_MS_MIN`, `DEFAULT_DELAY_MS_MAX`, and
`DEFAULT_CONTROL_POINTS` live in `src/humanize/mouse.py`.

## `src/humanize/typing.py` — Per-keystroke typing

`human_type(locator, text, ...)` types `text` one character at a time
through `page.keyboard.type(char, delay=ms)` instead of using
`locator.fill(text)`.

### What it does

- Focuses the target locator so each keystroke lands in the right
  element.
- For each character, samples a delay from
  `Normal(mean_ms, std_ms)`, floors it at `min_ms`, and rounds to an
  integer millisecond.
- Calls `page.keyboard.type(char, delay=...)` once per character —
  Playwright sleeps the configured delay *between* keystrokes.

`auth.login` in `src/vendors/ticketmaster/auth.py` replaces
`locator.fill(...)` with `human_type(...)` for credential fields when
`timing.humanize.typing.enabled: true`.

### Validation

The `dom.humanize-typing-keystrokes` assertion requires exactly one
`keydown` per character with at least 10 ms between consecutive
keys — the `min_ms` floor (default `20`) guarantees the lower bound.

### Config knobs

```yaml
timing:
  humanize:
    typing:
      enabled: true
      mean_ms: 70        # default DEFAULT_MEAN_MS
      std_ms: 25         # default DEFAULT_STD_MS
      min_ms: 20         # default DEFAULT_MIN_MS
```

Constants: `DEFAULT_MEAN_MS`, `DEFAULT_STD_MS`, `DEFAULT_MIN_MS` in
`src/humanize/typing.py`. Construction rejects non-positive `mean_ms`
and negative `std_ms` / `min_ms` via `ValueError`.

## `src/humanize/warmer.py` — Pre-flight cookie warmer

`warm(context, target_seconds, pages, seed)` opens 2-3 unrelated
Ticketmaster-style pages in the caller-supplied `BrowserContext`,
dwells on each for a randomised fraction of `target_seconds`, and
closes the intermediate pages before returning.

### What it does

- `_choose_visits(pages, rng)` picks 2-3 distinct URLs from the
  candidate pool.
- `_allocate_dwell_seconds(total, n, rng)` distributes the total dwell
  budget across the visits with a `±25%` per-slot jitter, then
  rescales so the sum exactly matches `target_seconds`.
- For each chosen URL, `_visit(...)` opens a new page, navigates with
  a 15-second `_GOTO_TIMEOUT_MS` budget, sleeps the assigned dwell,
  and closes the page in a `finally` block.

The warmer returns the list of URLs that successfully loaded so the
caller can log it; URLs that 404 or timeout are recorded at debug
level and skipped.

### Validation

The `dom.humanize-warmer-multi-page` assertion requires at least two
distinct top-level URL navigations before `warm` returns — the
`MIN_VISITS = 2` constant in `src/humanize/warmer.py` makes that the
floor.

### Config knobs

```yaml
timing:
  humanize:
    warmer:
      enabled: true
      target_seconds: 30      # default DEFAULT_TARGET_SECONDS
      pages:
        - "https://www.ticketmaster.com/"
        - "https://www.ticketmaster.com/events"
        - "https://www.ticketmaster.com/sports"
```

Constants: `DEFAULT_PAGES`, `DEFAULT_TARGET_SECONDS`, `MIN_VISITS`,
`MAX_VISITS` in `src/humanize/warmer.py`. Empty `pages` lists and
negative `target_seconds` raise `ValueError`.

## `src/humanize/profile.py` — Desktop / mobile fingerprint

The profile module owns the small set of Playwright context options
that distinguish a desktop session from a touch-driven mobile one:
`viewport`, `is_mobile`, `has_touch`, and (mobile only) `user_agent`.

### What it does

- `apply_desktop_profile(options)` sets `is_mobile=False`,
  `has_touch=False`, and defaults `viewport` to `1366x900` if missing.
- `apply_mobile_profile(options)` overwrites `viewport` to `390x844`,
  sets `is_mobile=True`, `has_touch=True`, and defaults the
  user agent to `MOBILE_USER_AGENT` (iPhone 14 Safari) if missing.
- `apply_profile(name, options)` dispatches by string name. Unknown
  names raise `ValueError`.

`src/vendors/ticketmaster/core.py` calls `apply_profile(cfg.browser.profile, options)`
right before `launch_persistent_context(...)` so the chosen profile
flags slot into the launch kwargs.

### Validation

The `dom.humanize-profile-mobile` assertion requires
`window.matchMedia('(pointer: coarse)').matches` to be `True` and
`navigator.maxTouchPoints > 0` after the mobile profile is applied.
The `is_mobile=True` + `has_touch=True` Playwright pair drives both
checks.

### Config knobs

```yaml
browser:
  profile: "desktop"   # or "mobile"
```

Constants in `src/humanize/profile.py`: `DESKTOP_VIEWPORT`,
`MOBILE_VIEWPORT`, `MOBILE_USER_AGENT`, `VALID_PROFILES`.

## Interaction with stealth

`src/utils/stealth.py` is independent of the humanise package but
applied through the same code path in
`src/vendors/ticketmaster/core.py`. The `apply_stealth(context, cfg)`
call installs the `_STEALTH_INIT_SCRIPT` (patches `navigator.webdriver`,
`window.chrome`, plugin/MIME arrays, the Permissions API, and the
WebGL vendor / renderer) plus any caller-supplied
`extra_http_headers`. The contract is documented at the top of
`src/utils/stealth.py`: defense-in-depth, not a silver bullet.

`config.browser.stealth.enabled: false` skips the init script entirely
so debugging sessions can run with a vanilla automation profile.

## Putting it all together

The full set of switches an operator may set under `timing.humanize` in
`config/config.yaml`:

```yaml
timing:
  humanize:
    enabled: true
    mouse:
      enabled: true
      steps_min: 20
      steps_max: 40
      delay_ms_min: 8
      delay_ms_max: 25
    typing:
      enabled: true
      mean_ms: 70
      std_ms: 25
      min_ms: 20
    warmer:
      enabled: true
      target_seconds: 30
      pages:
        - "https://www.ticketmaster.com/"
        - "https://www.ticketmaster.com/events"
        - "https://www.ticketmaster.com/sports"

browser:
  profile: "desktop"   # or "mobile"
  stealth:
    enabled: true
    user_agent: null
    extra_http_headers: {}
    webgl_vendor: "Intel Inc."
    webgl_renderer: "Intel Iris OpenGL Engine"
    viewport_jitter: 30
```

Every key listed here is parsed by `src/utils/config_loader.py` and
read from one of the source files referenced above. The defaults in
`config/config.yaml` ship with `humanize.enabled: false` so the bot is
not silently slower than necessary on first run — operators flip the
flag on once they want the realistic-pace mode.
