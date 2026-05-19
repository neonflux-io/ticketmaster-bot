# Vendors

A "vendor" is a ticketing platform the bot can drive (Ticketmaster, AXS,
SeeTickets, …). Each vendor lives under `src/vendors/<name>/` and
implements `VendorAdapter` from `src/vendors/base.py`. Today the only
shipped vendor is Ticketmaster (`src/vendors/ticketmaster/`); this
document describes the contract so adding a new one is a one-package
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
