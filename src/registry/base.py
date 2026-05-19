"""Generic plugin registry with entry-point auto-discovery.

The :class:`Registry` is parameterised by the type ``T`` of the values it
stores. Each instance carries a ``kind`` label (used in error messages) and
an optional ``entry_point_group``. When an entry-point group is provided,
the first call to :meth:`get` or :meth:`all` walks
``importlib.metadata.entry_points(group=...)`` and registers every advertised
plugin. Discovery runs at most once per registry instance.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from importlib import metadata as importlib_metadata
from typing import Generic, TypeVar

T = TypeVar("T")

log = logging.getLogger("ticketmaster-bot")


class RegistryError(Exception):
    """Base class for all registry-related errors."""

    def __init__(self, message: str, *, kind: str, name: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.name = name


class DuplicateRegistration(RegistryError):
    """Raised when the same name is registered twice in the same registry."""


class NotRegistered(RegistryError):
    """Raised when ``get(name)`` is called for a name that was never registered."""


class Registry(Generic[T]):
    """Type-safe plugin registry.

    Parameters
    ----------
    kind:
        Short label identifying this registry (e.g. ``"strategies"``). Used
        in the messages of :class:`DuplicateRegistration` and
        :class:`NotRegistered`.
    entry_point_group:
        Optional ``importlib.metadata`` entry-point group to auto-discover
        plugins from. When set, discovery runs lazily on the first call to
        :meth:`get` or :meth:`all`.
    """

    def __init__(self, kind: str, *, entry_point_group: str | None = None) -> None:
        self.kind = kind
        self.entry_point_group = entry_point_group
        self._items: dict[str, T] = {}
        self._discovered = False

    # -- mutation -----------------------------------------------------------

    def register(self, name: str, obj: T) -> T:
        """Register ``obj`` under ``name``.

        Raises :class:`DuplicateRegistration` if ``name`` is already taken.
        Returns ``obj`` unchanged so this method can be used as a decorator
        factory (``reg.register("foo", MyClass)``).
        """
        if name in self._items:
            raise DuplicateRegistration(
                f"{self.kind!s} registry already has an entry named {name!r}",
                kind=self.kind,
                name=name,
            )
        self._items[name] = obj
        return obj

    def unregister(self, name: str) -> None:
        """Remove ``name`` from the registry. No-op if not present."""
        self._items.pop(name, None)

    def clear(self) -> None:
        """Remove every entry from this registry."""
        self._items.clear()

    # -- access -------------------------------------------------------------

    def get(self, name: str) -> T:
        """Return the value registered under ``name``.

        Raises :class:`NotRegistered` (whose message contains ``name`` and
        the registry's ``kind``) if no such entry exists.
        """
        self._ensure_discovered()
        try:
            return self._items[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._items)) or "<none>"
            raise NotRegistered(
                f"{self.kind!s} registry has no entry named {name!r} "
                f"(available: {available})",
                kind=self.kind,
                name=name,
            ) from exc

    def all(self) -> dict[str, T]:
        """Return a shallow copy of all currently registered entries."""
        self._ensure_discovered()
        return dict(self._items)

    def __contains__(self, name: object) -> bool:
        self._ensure_discovered()
        return isinstance(name, str) and name in self._items

    def __iter__(self) -> Iterator[str]:
        self._ensure_discovered()
        return iter(list(self._items))

    def __len__(self) -> int:
        self._ensure_discovered()
        return len(self._items)

    # -- entry-point discovery ---------------------------------------------

    def _ensure_discovered(self) -> None:
        if self._discovered or self.entry_point_group is None:
            return
        # Mark discovery as having run *before* iterating, so a failure in
        # one plugin doesn't cause us to keep retrying on every access.
        self._discovered = True
        try:
            entries = importlib_metadata.entry_points(group=self.entry_point_group)
        except Exception:  # noqa: BLE001
            log.warning(
                "Entry-point discovery failed for group %r",
                self.entry_point_group,
            )
            return
        for entry in entries:
            if entry.name in self._items:
                # Local registrations win. Skip without raising so plugins
                # can't break a host application that already registered
                # the same logical name.
                log.debug(
                    "Skipping entry point %s (group %s); %r already registered locally",
                    entry,
                    self.entry_point_group,
                    entry.name,
                )
                continue
            try:
                obj = entry.load()
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to load entry point %s for group %s",
                    entry,
                    self.entry_point_group,
                )
                continue
            self._items[entry.name] = obj
            log.debug(
                "Loaded entry point %s (group %s) into %s registry",
                entry,
                self.entry_point_group,
                self.kind,
            )
