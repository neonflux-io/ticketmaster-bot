"""Per-account proxy assignment for Playwright contexts.

The submodule exposes :class:`src.proxy.manager.ProxyManager`, which turns
a configured list of proxy URLs and an assignment policy
(``sticky`` or ``round_robin``) into the small kwarg dict Playwright's
``launch_persistent_context`` expects: ``{"server": ..., "username": ...,
"password": ...}``.
"""

from __future__ import annotations

from .manager import ProxyManager, parse_proxy_url

__all__ = ["ProxyManager", "parse_proxy_url"]
