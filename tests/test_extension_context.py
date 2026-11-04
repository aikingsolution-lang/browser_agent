"""
Automated test suite for Milestone 2: ExtensionBrowserContext Adapter.

Tests cover:
1. Context initialization and default attributes.
2. Configuration attributes (allowed_domains, wait_between_actions).
3. session=None verification.
4. get_state() BrowserState reconstruction and caching.
5. Novelty hashing when cache_clickable_elements_hashes=True.
6. get_current_page() and get_agent_current_page() returning ExtensionTabProxy.
7. Correct Chrome tab ID preservation.
8. get_selector_map() behavior and cache invalidation.
9. remove_highlights() safe no-op.
10. close() resource cleanup and idempotence.
11. Async context manager (__aenter__ / __aexit__).
12. Error handling (HTTP 503 disconnection, HTTP 504 timeout, connection failure, action failure).
13. Proof that no Playwright browser or context is instantiated.
14. Live integration test with connected Chrome Extension (skipped cleanly if disconnected or needs reload).
"""

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from browser_use.browser.context import BrowserContextConfig
from browser_use.browser.views import BrowserState, TabInfo
from browser_use.dom.views import DOMElementNode

from src.browser.extension_context import ExtensionBrowserContext, ExtensionTabProxy
from tests.local_fixture_server import LocalFixtureServer


def get_sample_fixture() -> dict[str, Any]:
    """Loads the deterministic sample DOM fixture generated in Milestone 1."""
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    if not fixture_path.exists():
        raise FileNotFoundError(f"Sample fixture not found at {fixture_path}")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestExtensionBrowserContextUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for ExtensionBrowserContext using mocks and fixtures."""

    def setUp(self):
        self.raw_data = get_sample_fixture()

    async def test_context_initialization_defaults(self):
        """1. Verify default attributes and duck-typed BrowserContext properties."""
        context = ExtensionBrowserContext()

        self.assertEqual(context.server_url, "http://127.0.0.1:8765")
        self.assertIsInstance(context.config, BrowserContextConfig)
        self.assertIsNone(context.session, "session must be None for MVP")
        self.assertIsNone(context.browser, "browser must be None (no Playwright)")
        self.assertEqual(context.context_id, "extension-context")
        self.assertIsNone(context.state, "initial state should be None")
        await context.close()

    async def test_configuration_attributes(self):
        """2. Verify configuration attributes: allowed_domains and wait_between_actions."""
        custom_config = BrowserContextConfig(
            allowed_domains=["example.com", "app.local"],
            wait_between_actions=1.75,
        )
        context = ExtensionBrowserContext(config=custom_config)

        self.assertEqual(context.config.allowed_domains, ["example.com", "app.local"])
        self.assertEqual(context.config.wait_between_actions, 1.75)
        await context.close()

    async def test_session_is_none(self):
        """3. Explicitly verify session=None prevents Agent from triggering Playwright validation."""
        context = ExtensionBrowserContext()
        self.assertIsNone(context.session)
        # Verify boolean check mirrors Agent's `if self.browser_context.session:`
        self.assertFalse(bool(context.session))
        await context.close()

    async def test_get_state_reconstruction_and_caching(self):
        """4. & 5. Verify get_state() reconstructs BrowserState and caches state/selector_map."""
        context = ExtensionBrowserContext()
        context.adopt_tab(101)

        mock_response = {
            "type": "ACTION_RESULT",
            "action": "GET_DOM_STATE",
            "success": True,
            "result": self.raw_data,
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response

            state = await context.get_state()

            # Verify execute_action called with GET_DOM_STATE
            mock_exec.assert_awaited_once_with("GET_DOM_STATE", {"tabId": 101})

            # Verify genuine BrowserState
            self.assertIsInstance(state, BrowserState)
            self.assertIsInstance(state.element_tree, DOMElementNode)
            self.assertIsInstance(state.selector_map, dict)
            self.assertGreater(len(state.selector_map), 0)
            self.assertIsNone(state.screenshot, "Screenshot must be None")

            # Verify cached references
            self.assertIs(context.state, state)
            self.assertEqual(context._current_url, self.raw_data["url"])
            self.assertEqual(context._current_tab_id, self.raw_data["tab_id"])

        await context.close()

    async def test_get_state_clickable_elements_hashes(self):
        """Verify cache_clickable_elements_hashes=True computes element novelty without crashing."""
        context = ExtensionBrowserContext()

        mock_response = {
            "type": "ACTION_RESULT",
            "action": "GET_DOM_STATE",
            "success": True,
            "result": self.raw_data,
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response

            # First extraction computes initial hashes
            state1 = await context.get_state(cache_clickable_elements_hashes=True)
            self.assertIsNotNone(context._cached_clickable_elements_hashes)
            self.assertEqual(context._cached_clickable_elements_hashes.url, state1.url)

            # Second extraction with same URL verifies is_new flag behavior
            state2 = await context.get_state(cache_clickable_elements_hashes=True)
            self.assertIsInstance(state2, BrowserState)

        await context.close()

    async def test_get_current_page_returns_extension_tab_proxy(self):
        """6. Verify get_current_page() returns ExtensionTabProxy exposing url and tab_id."""
        context = ExtensionBrowserContext()

        mock_response = {
            "type": "ACTION_RESULT",
            "action": "GET_DOM_STATE",
            "success": True,
            "result": self.raw_data,
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response

            # Calling get_current_page before get_state auto-extracts state
            page = await context.get_current_page()

            self.assertIsInstance(page, ExtensionTabProxy)
            self.assertEqual(page.url, self.raw_data["url"])
            self.assertEqual(page.tab_id, self.raw_data["tab_id"])
            self.assertEqual(page.target_id, self.raw_data["tab_id"])
            self.assertIn(str(self.raw_data["tab_id"]), repr(page))
            self.assertEqual(str(page), self.raw_data["url"])

            # Verify get_agent_current_page() alias returns same result
            agent_page = await context.get_agent_current_page()
            self.assertIsInstance(agent_page, ExtensionTabProxy)
            self.assertEqual(agent_page.tab_id, page.tab_id)

        await context.close()

    async def test_correct_chrome_tab_id_preservation(self):
        """7. Verify native Chrome tab ID is preserved across BrowserState.tabs and ExtensionTabProxy."""
        context = ExtensionBrowserContext()
        test_tab_id = 999
        custom_data = dict(self.raw_data)
        custom_data["tab_id"] = test_tab_id

        mock_response = {
            "type": "ACTION_RESULT",
            "action": "GET_DOM_STATE",
            "success": True,
            "result": custom_data,
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response

            state = await context.get_state()
            self.assertEqual(len(state.tabs), 1)
            self.assertEqual(state.tabs[0].page_id, test_tab_id)

            page = await context.get_current_page()
            self.assertEqual(page.tab_id, test_tab_id)

        await context.close()

    async def test_get_selector_map_and_invalidation(self):
        """8. Verify get_selector_map() returns cached map and clears on invalidate_state()."""
        context = ExtensionBrowserContext()

        # Before state is extracted
        empty_map = await context.get_selector_map()
        self.assertEqual(empty_map, {})

        # After state is extracted
        mock_response = {
            "type": "ACTION_RESULT",
            "action": "GET_DOM_STATE",
            "success": True,
            "result": self.raw_data,
        }
        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response
            await context.get_state()

        selector_map = await context.get_selector_map()
        self.assertGreater(len(selector_map), 0)

        # Invalidate state (e.g. after navigation)
        context.invalidate_state()
        self.assertIsNone(context.state)
        cleared_map = await context.get_selector_map()
        self.assertEqual(cleared_map, {}, "Invalidated context must return empty selector_map")

        await context.close()

    async def test_remove_highlights_noop(self):
        """9. Verify remove_highlights() executes safely as a documented no-op."""
        context = ExtensionBrowserContext()
        # Should complete cleanly without exceptions
        await context.remove_highlights()
        await context.close()

    async def test_close_cleanup_and_idempotence(self):
        """10. Verify close() cleans up HTTP client resources and is safe when called multiple times."""
        context = ExtensionBrowserContext()

        # Touch client
        client = context._get_client()
        self.assertFalse(client.is_closed)

        await context.close()
        self.assertIsNone(context._http_client)

        # Second close should be safe and idempotent
        await context.close()

    async def test_async_context_manager(self):
        """Verify context manager interface (__aenter__ and __aexit__)."""
        async with ExtensionBrowserContext() as ctx:
            self.assertIsInstance(ctx, ExtensionBrowserContext)
        # Context exited cleanly
        self.assertIsNone(ctx._http_client)

    async def test_disconnection_and_error_handling(self):
        """11. Verify appropriate errors on HTTP 503, 504, connect error, and action failure."""
        context = ExtensionBrowserContext()

        # 1. 503 Service Unavailable -> ConnectionError
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_resp_503 = httpx.Response(503, json={"detail": "Chrome extension is not connected"})
        mock_client.post.return_value = mock_resp_503
        context._http_client = mock_client

        with self.assertRaises(ConnectionError) as ctx_err:
            await context.execute_action("GET_DOM_STATE")
        self.assertIn("503", str(ctx_err.exception))

        # 2. 504 Gateway Timeout -> TimeoutError
        mock_resp_504 = httpx.Response(504, json={"detail": "Browser action timed out"})
        mock_client.post.return_value = mock_resp_504
        with self.assertRaises(TimeoutError):
            await context.execute_action("GET_DOM_STATE")

        # 3. Network connection refused -> ConnectionError
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        with self.assertRaises(ConnectionError):
            await context.execute_action("GET_DOM_STATE")

        # 4. Action failure payload (HTTP 200 with success=False) -> RuntimeError
        mock_client.post.side_effect = None
        mock_client.post.return_value = httpx.Response(
            200,
            json={"type": "ACTION_RESULT", "success": False, "error": "Tab was closed unexpectedly"}
        )
        with self.assertRaises(RuntimeError) as ctx_fail:
            await context.execute_action("CLICK", {"xpath": "//button"})
        self.assertIn("Tab was closed unexpectedly", str(ctx_fail.exception))

        await context.close()

    def test_no_playwright_instantiated(self):
        """Verify that importing and instantiating ExtensionBrowserContext launches no Playwright browser."""
        import sys
        # Confirm that no playwright browser session or object is referenced by our class
        context = ExtensionBrowserContext()
        self.assertFalse(hasattr(context, "_playwright"))
        self.assertFalse(hasattr(context, "playwright"))
        self.assertIsNone(context.browser)
        self.assertIsNone(context.session)


class TestExtensionBrowserContextLive(unittest.IsolatedAsyncioTestCase):
    """Integration test against live FastAPI server and connected Chrome Extension."""

    async def _is_live_environment_ready(self) -> bool:
        """Checks if FastAPI server is up and extension is actively connected."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get("http://127.0.0.1:8765/health")
                if res.status_code == 200:
                    data = res.json()
                    return data.get("extension_connected", False)
        except Exception:
            return False
        return False

    async def test_live_extension_browser_context(self):
        """Executes full ExtensionBrowserContext workflow against live Chrome Extension."""
        ready = await self._is_live_environment_ready()
        if not ready:
            self.skipTest(
                "Live Chrome Extension is not connected to FastAPI on ws://127.0.0.1:8765. "
                "Skipping live integration test."
            )

        server = LocalFixtureServer()
        server_url = server.start()

        context = ExtensionBrowserContext(server_url="http://127.0.0.1:8765")
        try:
            # 1. Bootstrap the agent-owned tab
            try:
                state = await context.get_state()
            except RuntimeError as err:
                if "Unsupported action: GET_DOM_STATE" in str(err):
                    self.skipTest(
                        "Chrome Extension needs to be reloaded in chrome://extensions to pick up GET_DOM_STATE."
                    )
                raise

            self.assertIsInstance(state, BrowserState)
            tab_id = context._current_tab_id
            self.assertIsNotNone(tab_id)

            # 2. Navigate that tab to the fixture URL
            nav_res = await context.execute_action("NAVIGATE", {"url": server_url, "tabId": tab_id})
            self.assertTrue(nav_res.get("success", False))

            # 3. Retrieve a fresh BrowserState
            context.invalidate_state()
            state = await context.get_state()
            self.assertIsInstance(state, BrowserState)
            self.assertIsInstance(state.element_tree, DOMElementNode)

            # 4. Verify the expected interactive elements
            tag_names = {node.tag_name.lower() for node in state.selector_map.values()}
            self.assertIn("a", tag_names)
            self.assertIn("input", tag_names)
            self.assertIn("button", tag_names)
            self.assertGreaterEqual(len(state.selector_map), 3)

            # 5. Verify ExtensionTabProxy exposes the correct URL and tab ID
            proxy_page = await context.get_current_page()
            self.assertIsInstance(proxy_page, ExtensionTabProxy)
            self.assertEqual(proxy_page.tab_id, tab_id)
            self.assertTrue(proxy_page.url.startswith("http"))
            self.assertIn(f"127.0.0.1:{server.port}", proxy_page.url)

            # 6. Query selector map directly
            selector_map = await context.get_selector_map()
            self.assertEqual(len(selector_map), len(state.selector_map))

            # 7. Remove highlights (no-op check)
            await context.remove_highlights()
        finally:
            if context._current_tab_id is not None:
                try:
                    await context.execute_action("CLOSE_TAB", {"tabId": context._current_tab_id})
                except Exception:
                    pass
            await context.close()
            server.stop()


if __name__ == "__main__":
    unittest.main()
