"""
ExtensionBrowserContext adapter for Browser Use 0.1.48.

Implements a duck-typed browser_context compatible with Browser Use's agent
and controller interfaces without importing, initializing, or requiring Playwright.
All operations dispatch via HTTP RPC to the FastAPI server at /api/v1/action.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from browser_use.browser.context import (
    BrowserContextConfig,
    CachedStateClickableElementsHashes,
    ClickableElementProcessor,
)
from browser_use.browser.views import (
    BrowserState,
    TabInfo,
)
from browser_use.dom.views import DOMElementNode, SelectorMap

from src.browser.dom_reconstruction import (
    reconstruct_browser_state,
)


class ExtensionTabProxy:
    """
    Lightweight proxy exposing page attributes for Browser Use compatibility.
    Does NOT wrap a Playwright Page.
    """

    def __init__(self, tab_id: int, url: str = "", title: str = ""):
        self.tab_id = tab_id
        self.url = url
        self.title = title

    @property
    def target_id(self) -> int:
        """Alias for tab_id for compatibility."""
        return self.tab_id

    def __repr__(self) -> str:
        return f"ExtensionTabProxy(tab_id={self.tab_id}, url={self.url!r}, title={self.title!r})"

    def __str__(self) -> str:
        return self.url


class ExtensionBrowserContext:
    """
    Duck-typed BrowserContext adapter for Browser Use 0.1.48.
    
    Acts as a drop-in replacement for BrowserContext without launching or requiring Playwright.
    Communicates with the FastAPI WebSocket bridge via /api/v1/action.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8765",
        config: BrowserContextConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
        auth_token: str | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.config: BrowserContextConfig = config or BrowserContextConfig()

        # Token authentication for backend HTTP RPC requests
        self.auth_token = auth_token or os.getenv("EXTENSION_AUTH_TOKEN") or ""
        if not self.auth_token:
            try:
                from src.server.security import SESSION_AUTH_TOKEN
                self.auth_token = SESSION_AUTH_TOKEN
            except Exception:
                pass

        # MVP: session is explicitly None to bypass Playwright output validation in Browser Use Agent
        self.session = None

        # Duck-typed compatibility properties
        self.browser = None
        self.context_id: str = "extension-context"

        self._injected_client = http_client is not None
        self._http_client = http_client

        # Cached state tracking
        self._state: BrowserState | None = None
        self._current_tab_id: int | None = None
        self._current_url: str = ""
        self._current_title: str = ""
        self._cached_clickable_elements_hashes: CachedStateClickableElementsHashes | None = None

        # Strict agent-owned tab tracking (DEF-04)
        self.agent_tab_ids: set[int] = set()

    @property
    def state(self) -> BrowserState | None:
        """Returns the latest reconstructed BrowserState, or None if not yet fetched."""
        return self._state

    def adopt_tab(self, tab_id: int) -> None:
        """Explicitly authorizes and adopts a tab as an agent-owned tab."""
        self.agent_tab_ids.add(tab_id)
        self._current_tab_id = tab_id

    def _get_client(self) -> httpx.AsyncClient:
        """Returns active HTTP client or initializes a new one with auth headers."""
        if self._http_client is None or getattr(self._http_client, "is_closed", False) is True:
            headers = {}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            self._http_client = httpx.AsyncClient(
                base_url=self.server_url,
                headers=headers,
                timeout=httpx.Timeout(35.0, connect=5.0, read=35.0, write=10.0),
            )
        return self._http_client

    async def execute_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Dispatches an action to the FastAPI RPC endpoint at /api/v1/action.
        
        Handles connection failures, timeouts, authentication, and extension errors.
        """
        client = self._get_client()
        payload = {
            "action": action,
            "params": params or {},
        }
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            response = await client.post("/api/v1/action", json=payload, headers=headers)
        except httpx.ConnectError as err:
            raise ConnectionError(
                f"Could not connect to FastAPI server at {self.server_url}: {err}"
            ) from err
        except httpx.TimeoutException as err:
            raise TimeoutError(
                f"HTTP request timed out while executing action '{action}': {err}"
            ) from err

        if response.status_code == 401:
            detail = response.json().get("detail", "Unauthorized")
            raise PermissionError(f"HTTP RPC authentication failed (401): {detail}")
        elif response.status_code == 403:
            detail = response.json().get("detail", "Forbidden")
            raise PermissionError(f"HTTP RPC access forbidden (403): {detail}")
        elif response.status_code == 503:
            detail = response.json().get("detail", "Chrome extension is disconnected")
            raise ConnectionError(f"Extension backend unavailable (503): {detail}")
        elif response.status_code == 504:
            detail = response.json().get("detail", f"Action {action} timed out")
            raise TimeoutError(f"Browser action timeout (504): {detail}")
        elif response.status_code != 200:
            raise RuntimeError(
                f"Server returned HTTP {response.status_code} for action '{action}': {response.text}"
            )

        data = response.json()
        if not data.get("success", False):
            error_msg = data.get("error", "Unknown extension action error")
            raise RuntimeError(f"Extension action '{action}' failed: {error_msg}")

        return data

    async def get_state(
        self,
        cache_clickable_elements_hashes: bool = False,
    ) -> BrowserState:
        """
        Queries the Chrome Extension for live DOM state via GET_DOM_STATE,
        reconstructs a genuine BrowserState, updates tab/url caches, restricts
        visible tabs to agent-owned tabs, and optionally computes element novelty hashes.
        """
        if self._current_tab_id is not None:
            action_data = await self.execute_action("GET_DOM_STATE", {"tabId": self._current_tab_id})
        else:
            action_data = await self.execute_action("GET_DOM_STATE")
        raw_result = action_data.get("result", {})

        state = await reconstruct_browser_state(raw_result)

        # Prioritize actual inspected tab ID returned by FastAPI / Chrome Extension (DEF-02)
        inspected_tab_id = raw_result.get("tab_id")
        if inspected_tab_id is not None:
            self._current_tab_id = inspected_tab_id
            self.agent_tab_ids.add(inspected_tab_id)
        elif state.tabs:
            self._current_tab_id = state.tabs[0].page_id
            self.agent_tab_ids.add(self._current_tab_id)

        # Restrict agent-visible tabs to agent-owned or explicitly adopted tabs (DEF-04)
        if self.agent_tab_ids and state.tabs:
            state.tabs = [t for t in state.tabs if t.page_id in self.agent_tab_ids]

        # Find out which elements are new if requested and URL hasn't changed
        if cache_clickable_elements_hashes:
            if (
                self._cached_clickable_elements_hashes
                and self._cached_clickable_elements_hashes.url == state.url
            ):
                updated_clickable_elements = ClickableElementProcessor.get_clickable_elements(
                    state.element_tree
                )
                for dom_element in updated_clickable_elements:
                    dom_element.is_new = (
                        ClickableElementProcessor.hash_dom_element(dom_element)
                        not in self._cached_clickable_elements_hashes.hashes
                    )

            self._cached_clickable_elements_hashes = CachedStateClickableElementsHashes(
                url=state.url,
                hashes=ClickableElementProcessor.get_clickable_elements_hashes(state.element_tree),
            )

        self._state = state
        self._current_url = state.url
        self._current_title = state.title

        return state

    async def get_current_page(self) -> ExtensionTabProxy:
        """
        Returns a lightweight ExtensionTabProxy representing the active Chrome tab.
        Exposes url and the actual Chrome tab ID.
        """
        if self._state is None or self._current_tab_id is None:
            await self.get_state()

        return ExtensionTabProxy(
            tab_id=self._current_tab_id or 0,
            url=self._current_url,
            title=self._current_title,
        )

    async def get_agent_current_page(self) -> ExtensionTabProxy:
        """Alias for get_current_page() for maximum compatibility with Browser Use."""
        return await self.get_current_page()

    async def get_selector_map(self) -> SelectorMap:
        """
        Returns the selector_map from the latest BrowserState.
        Returns an empty dict if no state is cached or if state was invalidated.
        """
        if self._state is None:
            return {}
        return self._state.selector_map

    async def get_dom_element_by_index(self, index: int) -> DOMElementNode:
        """
        Resolves DOMElementNode from the cached selector map by its highlight index.
        """
        selector_map = await self.get_selector_map()
        if index not in selector_map:
            raise ValueError(f"Element with index {index} not found in selector_map")
        return selector_map[index]

    async def is_file_uploader(self, element_node: DOMElementNode) -> bool:
        """
        Checks whether the specified element node represents a file uploader.
        """
        return element_node.get_file_upload_element() is not None

    def invalidate_state(self) -> None:
        """
        Invalidates the cached browser state and selector map (e.g., following navigation).
        Ensures get_selector_map() cannot accidentally return stale data.
        """
        self._state = None
        self._cached_clickable_elements_hashes = None

    async def remove_highlights(self) -> None:
        """
        Safe documented no-op for now.
        Milestone 1 uses doHighlightElements=False, so no overlay elements or badges
        exist in the live DOM.
        """
        pass

    async def close(self) -> None:
        """
        Cleans up HTTP client resources.
        Does NOT close Chrome.
        Does NOT launch or terminate Playwright.
        """
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ExtensionBrowserContext:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
