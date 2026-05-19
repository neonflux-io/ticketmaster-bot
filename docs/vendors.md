# Vendors

A "vendor" is a ticketing platform the bot can drive (Ticketmaster US,
Ticketmaster SG, AXS, SeeTickets, …). Each vendor lives under
`src/vendors/<name>/` and implements `VendorAdapter` from
`src/vendors/base.py`. The shipped vendors today are:

- `ticketmaster` (the US/CA flow, `src/vendors/ticketmaster/`) — the
  reference implementation of the contract.
- `ticketmaster_sg` (the Singapore flow, `src/vendors/ticketmaster_sg/`)
  — a worked second-vendor example demonstrating per-vendor selectors,
  region-specific currency / date parsing, and a distinct captcha
  surface (Yii image CAPTCHA + invisible reCAPTCHA Enterprise + Queue-It).
  See [docs/vendors_ticketmaster_sg.md](vendors_ticketmaster_sg.md) for
  the operator-facing user guide.

This document describes the `VendorAdapter` contract and walks through
both shipped adapters as proof that adding a new one is a one-package
drop-in.

## The `VendorAdapter` contract

`src/vendors/base.py` defines an ABC with one abstract method
(`build_runner`) and six module attributes (`auth`, `navigator`,
`queue`, `cart`, `checkout`, `core`). The ABC deliberately exposes the
*module objects* used by the runner rather than redefining every step
on the adapter itself, so the existing state machine in
`src/vendors/ticketmaster/core.py` keeps its current shape:

```python
class VendorAdapter(ABC):
    name: str = ""

    auth: ModuleType
    navigator: ModuleType
    queue: ModuleType
    cart: ModuleType
    checkout: ModuleType
    core: ModuleType

    @abstractmethod
    def build_runner(
        self,
        config: BotConfig,
        account: AccountConfig | None = None,
    ) -> Any:
        """Return a runner whose ``async run()`` drives the flow."""
```

### Required attributes

| Attribute | Type | Purpose |
| --- | --- | --- |
| `name` | `str` | Stable lowercase identifier used by `src/registry/vendors.py`. |
| `auth` | module | Owns `is_logged_in(context)` and `login(context, account, …)`. |
| `navigator` | module | Owns `open_event(page, url, …)`, `detect_state(page)`, `wait_until_on_sale(page, …)`. |
| `queue` | module | Owns `wait_through_queue(page, …)`. |
| `cart` | module | Owns `set_quantity(page, n)` and `add_to_cart(page, …)`. |
| `checkout` | module | Owns `run_checkout(page, …)`. |
| `core` | module | Hosts the `BotRunner` class. |

The Ticketmaster adapter implementing this contract lives at
`src/vendors/ticketmaster/adapter.py` (`TicketmasterAdapter`) and pins
each attribute to the corresponding submodule (`src/vendors/ticketmaster/auth.py`,
`src/vendors/ticketmaster/navigator.py`, …).

### The runner contract

`build_runner(config, account=None)` must return an object whose
`async run()` coroutine returns a truthy value on success and falsy on
graceful failure. The production runner
(`src/vendors/ticketmaster/core.py::BotRunner`) wraps `_run` in a
`try / except / finally` that fires the `on_run_start`, `on_failure`,
and `on_run_end` lifecycle events from
`src/orchestrator/lifecycle.py`. Any new runner should follow the same
pattern so hooks fire predictably.

## How discovery works

Two layers cooperate to make a vendor reachable from the CLI:

1. **Package import side-effect.** `src/vendors/__init__.py` imports
   the bundled Ticketmaster package, which in turn calls
   `src.registry.vendors.register("ticketmaster", TicketmasterAdapter)`
   from `src/vendors/ticketmaster/__init__.py`. The registration is
   idempotent (`if "ticketmaster" not in registry` guard) so re-imports
   in tests do not raise `DuplicateRegistration`.

2. **Entry-point auto-discovery.** `src/registry/vendors.py` is a
   `Registry` instance bound to the `ticketmaster_bot.vendors`
   entry-point group. The first call to `registry.get(...)` or
   `registry.all()` walks `importlib.metadata.entry_points` and
   registers every advertised plugin. Local registrations win over
   entry points (the registry skips a duplicate name from a plugin
   with a debug log line).

The CLI selects an adapter via `--vendor <name>`; the resolver in
`src/main.py` calls `src.registry.vendors.get(args.vendor)`, which
raises `NotRegistered` with the unknown name in the message when the
name is bad. The validation contract assertion
`refactor.cli-vendor` pins this exit-code-2 + stderr behaviour.

When `--vendor` is **omitted**, `src/main.py::detect_vendor_for_url`
chooses an adapter by the host of the first configured event URL.
The mapping (`_HOST_VENDOR_MAP`) is small and ordered:

```python
_HOST_VENDOR_MAP = (("ticketmaster.sg", "ticketmaster_sg"),)
DEFAULT_VENDOR = "ticketmaster"
```

`ticketmaster.sg` and any subdomain thereof (`www.ticketmaster.sg`,
`my.ticketmaster.sg`) resolve to `ticketmaster_sg`; everything else —
including `ticketmaster.com` and `www.ticketmaster.com` — falls
through to the default US adapter. The auto-detection only fires
after configuration loads, so it sees the merged events list
including any `--events URL` overrides on the CLI.

## The Ticketmaster adapter

Shipped files under `src/vendors/ticketmaster/`:

- `adapter.py` — `TicketmasterAdapter`. Twenty-line class binding the
  per-step modules.
- `__init__.py` — registers `TicketmasterAdapter` in
  `src/registry/vendors.py` and re-exports the step modules.
- `auth.py` — login flow; integrates with `src/humanize/typing.py`
  when `timing.humanize.typing.enabled` is set.
- `navigator.py` — URL navigation, on-sale waiting, page-state
  detection (`queue`, `tickets`, `sold_out`, `not_on_sale`).
- `queue.py` — queue-wait loop with configurable check interval.
- `cart.py` — quantity selection and add-to-cart.
- `checkout.py` — payment + delivery + final order placement.
- `core.py` — the `BotRunner` state machine described in
  `docs/architecture.md`.

A backwards-compatibility shim lives at `src/bot/__init__.py` (referenced
by the `refactor.bot-shim` assertion) re-exporting these modules so any
external caller using the legacy `from src.bot import auth` form keeps
working.

## The Ticketmaster SG adapter (worked second-vendor example)

The Singapore adapter at `src/vendors/ticketmaster_sg/` is the
canonical example of how a second vendor slots into the existing
machinery. It is structurally identical to the US adapter — same
six per-step modules plus an `adapter.py` and `__init__.py` — but
every behaviour the SG site does differently is encoded in a
SG-specific module, never patched into the shared US code.

```
src/vendors/ticketmaster_sg/
├── __init__.py    # registers TicketmasterSGAdapter with the vendor registry
├── adapter.py     # class TicketmasterSGAdapter(VendorAdapter)
├── core.py        # BotRunner subclass: inherits the state machine, swaps the steps
├── auth.py        # SG OAuth via auth.ticketmaster.com (client_id=...tmsg)
├── navigator.py   # /activity/detail/<game> open + SG date parser
├── queue.py       # Queue-It handler (customer id = "ticketmasterasia")
├── cart.py        # button#autoMode + SG terms checkbox + marketing-optin guard
├── checkout.py    # SG delivery / saved-card / SGD cart verifier / Place Order
├── price.py       # SGD price parser (S$, SGD, SGD$, bare $)
└── selectors.py   # SG-scoped selector registry over ticketmaster_sg.yaml
```

### How it reuses the US machinery (and where it diverges)

The SG runner inherits the state machine outright:

```python
# src/vendors/ticketmaster_sg/core.py
from ..ticketmaster.core import BotRunner as _BaseBotRunner
from . import auth, cart, checkout, navigator, queue


class BotRunner(_BaseBotRunner):
    """SG-bound BotRunner. Inherits the state machine, swaps the steps."""

    auth_module = auth
    cart_module = cart
    checkout_module = checkout
    navigator_module = navigator
    queue_module = queue
```

Because the US `BotRunner` reads its step modules from instance
attributes (`auth_module`, `cart_module`, …), the SG subclass only
has to re-bind those attributes — every lifecycle event, hook
dispatch, proxy plumbing, stealth init, and run-artefacts wiring
fires identically for both vendors with zero duplication.

Where the SG flow diverges from the US flow, the divergence lives in
its own module:

| Concern | SG-specific module | What changed |
| --- | --- | --- |
| Selectors | `src/vendors/ticketmaster_sg/selectors.py` + `config/selectors/ticketmaster_sg.yaml` | SG uses Yii ids (`button#autoMode`, `select[name='TicketForm[count]']`, `input#TicketForm_verifyCode`) — zero `data-bdd` attributes. A dedicated selector registry sidesteps logical-name collisions with the US YAML. |
| Captcha | `src/vendors/ticketmaster_sg/auth.py` | Two systems run side-by-side on `/ticket/check-captcha/`: a Yii image CAPTCHA and an invisible reCAPTCHA Enterprise widget. `wait_for_human_if_captcha` covers both. |
| Login | `src/vendors/ticketmaster_sg/auth.py` | PingFederate OAuth with SG-specific `client_id` and `redirect_uri=identity.ticketmaster.sg/exchange`. `is_logged_in` validates the SG cookie set (`eps_sid`, `tmpt`, `TIXPUISID`), not the US set. |
| Date parsing | `src/vendors/ticketmaster_sg/navigator.py::parse_sg_date` | SG renders show times as `10 Dec 2026 (Thu.) 05:00 pm`; parser localises to Asia/Singapore (UTC+08:00, no DST). |
| State detection | `src/vendors/ticketmaster_sg/navigator.py::detect_state` | Adds SG-only states `captcha`, `login_required`, `login_in_progress`, `interactive_seatmap` keyed off URL prefixes (`/ticket/check-captcha/`, `identity.ticketmaster.sg/exchange`, …). |
| Currency parsing | `src/vendors/ticketmaster_sg/price.py::parse_sgd_price` | Understands `$X`, `S$X`, `SGD X`, `SGD$X`; falls back to the shared US `$X` extractor so the same parser still works for the bare-dollar shape. |
| Quantity | `src/vendors/ticketmaster_sg/cart.py::set_quantity` | SG sets quantity on the *pre-cart* `select#TicketForm_count`; the US site sets it on the cart-page `<select name="quantity">`. |
| Terms checkbox | `src/vendors/ticketmaster_sg/cart.py::accept_terms_if_needed` | SG's `input#TicketForm_agree` sits next to marketing opt-ins; the helper blocks marketing matches via an explicit blocklist of id/name/label keywords. |
| Queue | `src/vendors/ticketmaster_sg/queue.py` | Queue-It on `<event>.queue-it.net` (customer id `ticketmasterasia`) instead of TM's in-house Smart Queue. |
| Section parsing | `src/vendors/ticketmaster_sg/checkout.py::_extract_sg_sections` | SG sometimes prints multi-word section labels like `Section: GEN ADM`; the SG extractor accepts a trailing run of uppercase tokens and normalises by collapsing whitespace, so `GENADM` and `GEN ADM` compare equal. |

### Adapter registration

The SG adapter follows the same two-channel discovery pattern as the
US adapter:

```python
# src/vendors/ticketmaster_sg/__init__.py
from src.registry import vendors as _vendor_registry
from .adapter import TicketmasterSGAdapter

if "ticketmaster_sg" not in _vendor_registry.registry:
    _vendor_registry.register("ticketmaster_sg", TicketmasterSGAdapter)
```

Importing `src.vendors` (which `src/main.py` does once on startup)
imports both `src.vendors.ticketmaster` and
`src.vendors.ticketmaster_sg`; each subpackage registers its adapter
on import as a side-effect. The `ticketmaster_bot.vendors`
entry-point group remains the external plugin path for adapters
shipped by other distributions.

### Host-based vendor auto-detection

`src/main.py::detect_vendor_for_url` reads the host of the first
configured event URL and picks the SG adapter when the host is
`ticketmaster.sg` (or a subdomain). The mapping is documented
above under **How discovery works**. As a result, an end user
typically does not need to pass `--vendor` at all:

```bash
# Auto-detects ticketmaster_sg.
python run.py --events https://ticketmaster.sg/activity/detail/26sg_pglcs2major

# Explicit override (used by --dry-run / --explain when the loaded
# config has a placeholder ticketmaster.com URL).
python run.py --vendor ticketmaster_sg --dry-run
```

See [docs/vendors_ticketmaster_sg.md](vendors_ticketmaster_sg.md)
for the operator-facing details (SG configuration knobs, captcha
workflow, regional payment notes).

## Adding a new vendor

The smallest possible new vendor — call it `<vendor>` — is a single
sibling package directory under `src/vendors/`:

```
src/vendors/<vendor>/
├── __init__.py     # registers <Vendor>Adapter with src/registry/vendors.py
├── adapter.py      # class <Vendor>Adapter(VendorAdapter)
├── auth.py         # async def is_logged_in(context), async def login(...)
├── navigator.py    # async def open_event(...), async def detect_state(...)
├── queue.py        # async def wait_through_queue(...)
├── cart.py         # async def set_quantity(...), async def add_to_cart(...)
├── checkout.py     # async def run_checkout(...)
└── core.py         # class BotRunner: async def run(self) -> bool
```

Concrete checklist:

1. **Write the per-step modules.** Each one targets the new vendor's
   actual DOM. Selectors must come from a new YAML file under
   `config/selectors/` (named after the vendor) accessed via
   `src/registry/selectors.py` — never inline `[data-bdd=...]` strings
   in Python.

2. **Write the adapter class.** Mirror
   `src/vendors/ticketmaster/adapter.py`: set `name = "<vendor>"`,
   pin the per-step modules in `__init__`, and implement
   `build_runner(config, account=None)` to return the new `BotRunner`.

3. **Write the runner.** Use `src/vendors/ticketmaster/core.py` as the
   template. Re-use:
   - `src/orchestrator/lifecycle.py::LifecycleDispatcher` for hook
     dispatch.
   - `src/humanize/profile.py::apply_profile` for the desktop/mobile
     fingerprint.
   - `src/proxy/manager.py::ProxyManager` for proxy plumbing.
   - `src/utils/stealth.py::apply_stealth` for stealth init scripts.
   - `src/utils/run_artifacts.py::RunArtifactDir` for HAR + failure
     dumps.

4. **Register the adapter.** Add this to the new vendor package's
   `__init__.py` (mirroring `src/vendors/ticketmaster/__init__.py`):

   ```python
   from src.registry import vendors as _vendor_registry
   from .adapter import MyVendorAdapter

   if "myvendor" not in _vendor_registry.registry:
       _vendor_registry.register("myvendor", MyVendorAdapter)
   ```

5. **Import the package on bootstrap.** Append the package to
   `src/vendors/__init__.py` so the registration side effect fires
   when `src.vendors` is imported (matches the Ticketmaster pattern).

6. **Externally**: another distribution may register an adapter
   through the `ticketmaster_bot.vendors` entry-point group without
   touching the host repo. Example `pyproject.toml`:

   ```toml
   [project.entry-points."ticketmaster_bot.vendors"]
   myvendor = "my_package.adapter:MyVendorAdapter"
   ```

7. **Add a real-Chromium test.** Drop fixture HTML at
   `tests/fixtures/<vendor>/...` and write a test that drives the new
   adapter end-to-end through the local file:// URL. Re-use the
   `chromium` and `chromium_context` fixtures from
   `tests/conftest.py`.

8. **Document the new adapter** by adding a section to this file with
   any vendor-specific quirks (queue release semantics, payment-page
   timing, captcha vectors, …) and updating
   `config/config.yaml` with any new top-level keys.

The `VendorAdapter` ABC and the registry + entry-point machinery are
the only things a new vendor must consume from the host repo. Nothing
under `src/strategies/`, `src/notifiers/`, `src/hooks/`, or
`src/humanize/` is vendor-specific — that code keeps working across
all vendors as long as the selector logical names exist in the new
vendor's `config/selectors/<vendor>.yaml`.
