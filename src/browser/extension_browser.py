"""
ExtensionBrowser proxy for Browser Use 0.1.48 integration with Chrome Extension.

Provides a duck-typed browser proxy compatible with BrowserUseAgent without
inheriting from Browser, without importing Playwright, and without launching
any Chromium or browser processes.
"""

from __future__ import annotations

import logging
from typing import Any

from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContextConfig

logger = logging.getLogger(__name__)


class ExtensionBrowser:
    """
    Lightweight duck-typed browser proxy representing the external Chrome browser.
    
    Responsibilities:
    - Expose `config: BrowserConfig` expected by Browser Use Agent.
    - Provide safe async no-op `_init()` so Agent does not start Playwright.
    - Provide safe async no-op `close()` that does not terminate Chrome.
    - Create new `ExtensionBrowserContext` instances via `new_context()`.
    """

    def __init__(self, config: BrowserConfig | None = None):
        self.config: BrowserConfig = config or BrowserConfig()
        self.playwright = None

    async def _init(self) -> None:
        """Safe no-op. The real Chrome browser is managed externally by the user/extension."""
        pass

    async def close(self) -> None:
        """Safe no-op. Does not terminate the user's Chrome browser."""
        pass

    async def new_context(
        self,
        config: BrowserContextConfig | None = None,
        server_url: str = "http://127.0.0.1:8765",
    ):
        """Creates and returns an ExtensionBrowserContext attached to this browser proxy."""
        from src.browser.extension_context import ExtensionBrowserContext

        context_config = config or self.config.new_context_config
        return ExtensionBrowserContext(server_url=server_url, config=context_config)

    async def __aenter__(self) -> ExtensionBrowser:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
