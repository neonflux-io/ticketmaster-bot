"""Shared pytest fixtures + fakes for Playwright Page/Locator interfaces."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Make `src` importable when running pytest from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# --- Minimal async fake of a Playwright Locator + Page ---


class FakeLocator:
    """Lightweight stand-in for playwright.async_api.Locator."""

    def __init__(
        self,
        text: str = "",
        *,
        visible: bool = True,
        attrs: dict[str, str] | None = None,
        children: list[FakeLocator] | None = None,
    ) -> None:
        self._text = text
        self._visible = visible
        self._attrs = attrs or {}
        self._children = children or []
        self.click_count = 0
        self.scrolled = False
        self.checked = False
        self.selected_value: str | None = None

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _i: int) -> FakeLocator:
        if self._children:
            return self._children[_i]
        return self

    async def count(self) -> int:
        return len(self._children)

    async def inner_text(self, timeout: int | None = None) -> str:  # noqa: ARG002
        return self._text

    async def is_visible(self, timeout: int | None = None) -> bool:  # noqa: ARG002
        return self._visible

    async def is_checked(self) -> bool:
        return self.checked

    async def check(self) -> None:
        self.checked = True

    async def click(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self.click_count += 1

    async def scroll_into_view_if_needed(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self.scrolled = True

    async def wait_for(self, **_: Any) -> None:
        return None

    async def get_attribute(
        self, name: str, timeout: int | None = None
    ) -> str | None:  # noqa: ARG002
        return self._attrs.get(name)

    async def select_option(self, value: str) -> None:
        self.selected_value = value

    def locator(self, _sel: str) -> FakeLocator:
        return self


class FakePage:
    """Tiny Playwright Page substitute - returns a fixed locator per selector."""

    def __init__(
        self,
        locators: dict[str, FakeLocator] | None = None,
        url: str = "https://www.ticketmaster.com/event/X",
        title: str = "Event - Ticketmaster",
    ) -> None:
        self._locators = locators or {}
        self.url = url
        self._title = title
        self.frames: list[Any] = []
        self.content_text = ""

    def locator(self, selector: str) -> FakeLocator:
        return self._locators.get(selector, FakeLocator(visible=False))

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self.content_text
