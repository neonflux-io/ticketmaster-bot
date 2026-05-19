"""Browser stealth helpers - patch automation tells before any page script runs.

These mitigations are *not* a guarantee against modern bot detection (Imperva,
Datadome, Akamai). They simply remove the most obvious automation signals so
the rest of the flow has a fair chance. Treat as defense-in-depth, not a
silver bullet.
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

    from .config_loader import StealthConfig

log = logging.getLogger("ticketmaster-bot")


# Small, frequently-updated pool of real Chrome desktop UAs (macOS + Windows).
DEFAULT_UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


_STEALTH_INIT_SCRIPT = r"""
(() => {
  try {
    // 1. navigator.webdriver
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });

    // 2. window.chrome shim
    if (!window.chrome) {
      window.chrome = { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} };
    }

    // 3. plugin & mimeType arrays - non-empty looks more human
    const fakePlugin = (name, filename, desc) => {
      const plugin = Object.create(Plugin.prototype);
      Object.defineProperties(plugin, {
        name: { value: name },
        filename: { value: filename },
        description: { value: desc },
        length: { value: 1 },
      });
      return plugin;
    };
    const pluginArray = [
      fakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    ];
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => pluginArray,
      configurable: true,
    });
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['en-US', 'en'],
      configurable: true,
    });

    // 4. Permissions API - Notification.permission should match navigator.permissions
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
      navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : origQuery.call(navigator.permissions, params);
    }

    // 5. WebGL vendor / renderer spoof
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return '__WEBGL_VENDOR__';
      if (parameter === 37446) return '__WEBGL_RENDERER__';
      return getParam.call(this, parameter);
    };
    if (window.WebGL2RenderingContext) {
      const getParam2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function (parameter) {
        if (parameter === 37445) return '__WEBGL_VENDOR__';
        if (parameter === 37446) return '__WEBGL_RENDERER__';
        return getParam2.call(this, parameter);
      };
    }

    // 6. Hide that we patched these methods.
    const toStringFn = Function.prototype.toString;
    Function.prototype.toString = function () {
      if (this === WebGLRenderingContext.prototype.getParameter) {
        return 'function getParameter() { [native code] }';
      }
      if (this === navigator.permissions.query) {
        return 'function query() { [native code] }';
      }
      return toStringFn.call(this);
    };
  } catch (err) {
    /* swallow - never break the page */
  }
})();
"""


def random_ua() -> str:
    return random.choice(DEFAULT_UA_POOL)


def jittered_viewport(
    base_width: int = 1366,
    base_height: int = 900,
    jitter: int = 30,
) -> dict[str, int]:
    if jitter <= 0:
        return {"width": base_width, "height": base_height}
    return {
        "width": base_width + random.randint(-jitter, jitter),
        "height": base_height + random.randint(-jitter, jitter),
    }


async def apply_stealth(
    context: BrowserContext,
    config: StealthConfig,
) -> None:
    """Install the stealth init script and extra HTTP headers."""
    if not config.enabled:
        return

    script = (
        _STEALTH_INIT_SCRIPT
        .replace("__WEBGL_VENDOR__", _escape_js(config.webgl_vendor))
        .replace("__WEBGL_RENDERER__", _escape_js(config.webgl_renderer))
    )
    try:
        await context.add_init_script(script)
    except Exception as exc:  # noqa: BLE001
        log.debug("Stealth init script failed to attach: %s", exc)

    if config.extra_http_headers:
        try:
            await context.set_extra_http_headers(config.extra_http_headers)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not set extra http headers: %s", exc)


def _escape_js(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
