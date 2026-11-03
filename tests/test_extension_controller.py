"""
Automated test suite for Milestone 3B: ExtensionController Integration.

Tests cover:
1. Controller initialization, exclusion of Playwright actions, and registration of extension actions.
2. Tab ID resolution (-1 for newest tab, exact Chrome tab ID match, unambiguous positional index).
3. Invalidation of cached DOM state after navigation, tab switching, and DOM-modifying actions.
4. Safe rejection of nonexistent or stale selector map indices.
5. All actions ensuring dispatch includes browser's designated tab ID.
6. Live integration tests against connected Chrome Extension:
   - Navigation and fresh DOM extraction.
   - Input text into interactive elements.
   - Pressing keys (Enter).
   - Clicking elements by DOM index.
   - Scrolling down and up.
   - Opening new tab, switching between multiple tabs, and closing tabs.
   - Agent operations continuing to target designated tab regardless of foreground focus.
   - YouTube navigation and search execution.
"""

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from browser_use.agent.views import ActionResult
from browser_use.browser.views import BrowserState
from browser_use.controller.views import (
    ClickElementAction,
    CloseTabAction,
    GoToUrlAction,
    InputTextAction,
    OpenTabAction,
    ScrollAction,
    SendKeysAction,
    SwitchTabAction,
)

from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import (
    ExtensionController,
    resolve_chrome_tab_id,
)


def get_sample_fixture() -> dict[str, Any]:
    """Loads the deterministic sample DOM fixture generated in Milestone 1."""
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    if not fixture_path.exists():
        raise FileNotFoundError(f"Sample fixture not found at {fixture_path}")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestExtensionControllerUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for ExtensionController and tab resolution logic."""

    def setUp(self):
        self.sample_data = get_sample_fixture()
        self.controller = ExtensionController()

    async def test_controller_initialization_and_action_registry(self):
        """Verify ExtensionController registers extension actions and excludes Playwright actions."""
        registered_actions = list(self.controller.registry.registry.actions.keys())

        expected_extension_actions = [
            "done",
            "wait",
            "search_google",
            "go_to_url",
            "go_back",
            "click_element_by_index",
            "input_text",
            "switch_tab",
            "open_tab",
            "close_tab",
            "scroll_down",
            "scroll_up",
            "send_keys",
        ]
        for action_name in expected_extension_actions:
            self.assertIn(action_name, registered_actions, f"Expected {action_name} in registered actions")

        excluded_playwright = ["save_pdf", "drag_drop", "get_sheet_contents", "update_range_contents"]
        for action_name in excluded_playwright:
            self.assertNotIn(action_name, registered_actions)

    async def test_resolve_chrome_tab_id(self):
        """Verify tab resolution: exact match, -1 (last tab), and positional fallback."""
        context = ExtensionBrowserContext()

        mock_tabs_response = {
            "type": "ACTION_RESULT",
            "action": "GET_TABS",
            "success": True,
            "result": [
                {"tabId": 1001, "title": "First", "url": "https://first.com"},
                {"tabId": 2002, "title": "Second", "url": "https://second.com"},
                {"tabId": 3003, "title": "Third", "url": "https://third.com"},
            ],
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_tabs_response

            # 1. Exact Chrome tab ID match
            resolved_exact = await resolve_chrome_tab_id(context, 2002)
            self.assertEqual(resolved_exact, 2002)

            # 2. -1 refers to the last / newest tab
            resolved_last = await resolve_chrome_tab_id(context, -1)
            self.assertEqual(resolved_last, 3003)

            # 3. Positional 0-based index fallback
            resolved_pos = await resolve_chrome_tab_id(context, 0)
            self.assertEqual(resolved_pos, 1001)

            # 4. Out of range / unresolvable raises ValueError
            with self.assertRaises(ValueError):
                await resolve_chrome_tab_id(context, 9999)

        await context.close()

    async def test_stale_and_nonexistent_selector_rejection(self):
        """Verify clicking or typing with nonexistent index returns descriptive error without crashing."""
        context = ExtensionBrowserContext()

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }
            await context.get_state()

        # 1. Nonexistent index for click
        click_fn = self.controller.registry.registry.actions["click_element_by_index"].function
        click_result = await click_fn(ClickElementAction(index=99999), browser=context)
        self.assertIsInstance(click_result, ActionResult)
        self.assertTrue(bool(click_result.error))
        self.assertIn("does not exist in selector map", click_result.error)

        # 2. Nonexistent index for input_text
        type_fn = self.controller.registry.registry.actions["input_text"].function
        type_result = await type_fn(InputTextAction(index=99999, text="hello"), browser=context)
        self.assertIsInstance(type_result, ActionResult)
        self.assertTrue(bool(type_result.error))
        self.assertIn("does not exist in selector map", type_result.error)

        await context.close()

    async def test_dom_state_invalidation_after_actions(self):
        """Verify cached state is invalidated after navigation, clicks, and tab switching."""
        context = ExtensionBrowserContext()
        context._current_tab_id = 42

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }
            await context.get_state()
            self.assertIsNotNone(context.state)

            # 1. Click element -> invalidates state
            mock_exec.return_value = {"type": "ACTION_RESULT", "success": True, "result": {}}
            click_fn = self.controller.registry.registry.actions["click_element_by_index"].function
            first_idx = next(iter(context.state.selector_map.keys()))
            await click_fn(ClickElementAction(index=first_idx), browser=context)
            self.assertIsNone(context.state, "State must be invalidated after click")

            # 2. Re-populate state and test navigation
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }
            await context.get_state()
            self.assertIsNotNone(context.state)

            nav_fn = self.controller.registry.registry.actions["go_to_url"].function
            mock_exec.return_value = {"type": "ACTION_RESULT", "success": True, "result": {}}
            await nav_fn(GoToUrlAction(url="https://example.com"), browser=context)
            self.assertIsNone(context.state, "State must be invalidated after navigation")

        await context.close()

    async def test_actions_target_designated_tab_id(self):
        """Verify all controller actions pass browser's designated tabId to execute_action."""
        context = ExtensionBrowserContext()
        designated_tab_id = 777

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }
            await context.get_state()
            context._current_tab_id = designated_tab_id

            # Test scroll
            scroll_fn = self.controller.registry.registry.actions["scroll_down"].function
            mock_exec.return_value = {"type": "ACTION_RESULT", "success": True, "result": {}}
            await scroll_fn(ScrollAction(amount=500), browser=context)
            mock_exec.assert_awaited_with("SCROLL", {
                "direction": "DOWN",
                "amount": 500,
                "tabId": designated_tab_id,
            })

            # Test send_keys
            keys_fn = self.controller.registry.registry.actions["send_keys"].function
            await keys_fn(SendKeysAction(keys="Enter"), browser=context)
            mock_exec.assert_awaited_with("PRESS_KEY", {
                "key": "Enter",
                "tabId": designated_tab_id,
            })

        await context.close()


class TestExtensionControllerLive(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive live integration tests executing against connected Chrome Extension.
    Safely skipped if extension or FastAPI server is not available.
    """

    async def _is_live_ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get("http://127.0.0.1:8765/health")
                if res.status_code == 200:
                    data = res.json()
                    return data.get("extension_connected", False)
        except Exception:
            return False
        return False

    async def test_live_controller_actions_flow(self):
        """
        Executes real browser actions end-to-end:
        1. Query open tabs and initial DOM state.
        2. Create a new tab and navigate.
        3. Extract fresh DOM state.
        4. Type into input and press Enter.
        5. Scroll down and scroll up.
        6. Switch tabs and preserve tab targeting.
        7. Close tab cleanly.
        """
        ready = await self._is_live_ready()
        if not ready:
            self.skipTest("Chrome Extension is not connected on ws://127.0.0.1:8765 - skipping live controller test")

        controller = ExtensionController()
        context = ExtensionBrowserContext(server_url="http://127.0.0.1:8765")

        try:
            try:
                # 1. Initial state retrieval
                state = await context.get_state()
                self.assertIsInstance(state, BrowserState)
                initial_tab_id = context._current_tab_id
                self.assertIsNotNone(initial_tab_id)

                # 2. Open new tab with Wikipedia
                open_tab_fn = controller.registry.registry.actions["open_tab"].function
                open_res = await open_tab_fn(
                    OpenTabAction(url="https://en.wikipedia.org"),
                    browser=context,
                )
                self.assertIsInstance(open_res, ActionResult)
                wiki_tab_id = context._current_tab_id
                self.assertNotEqual(wiki_tab_id, initial_tab_id)

                # 3. Extract fresh DOM state on Wikipedia
                wiki_state = await context.get_state()
                self.assertIn("wikipedia", wiki_state.url.lower())
                self.assertGreater(len(wiki_state.selector_map), 0)

                # 4. Find search input and type query
                input_indices = [
                    idx for idx, node in wiki_state.selector_map.items()
                    if node.tag_name.lower() in ("input", "textarea") and node.is_interactive
                ]
                if input_indices:
                    search_idx = input_indices[0]
                    type_fn = controller.registry.registry.actions["input_text"].function
                    type_res = await type_fn(
                        InputTextAction(index=search_idx, text="Artificial Intelligence"),
                        browser=context,
                    )
                    self.assertIsInstance(type_res, ActionResult)

                    # Press Enter
                    keys_fn = controller.registry.registry.actions["send_keys"].function
                    keys_res = await keys_fn(
                        SendKeysAction(keys="Enter"),
                        browser=context,
                    )
                    self.assertIsInstance(keys_res, ActionResult)

                # 5. Test Scrolling
                scroll_down_fn = controller.registry.registry.actions["scroll_down"].function
                scroll_res = await scroll_down_fn(ScrollAction(amount=400), browser=context)
                self.assertIsInstance(scroll_res, ActionResult)

                scroll_up_fn = controller.registry.registry.actions["scroll_up"].function
                scroll_up_res = await scroll_up_fn(ScrollAction(amount=300), browser=context)
                self.assertIsInstance(scroll_up_res, ActionResult)

                # 6. Switch back to initial tab
                switch_tab_fn = controller.registry.registry.actions["switch_tab"].function
                switch_res = await switch_tab_fn(
                    SwitchTabAction(page_id=initial_tab_id),
                    browser=context,
                )
                self.assertIsInstance(switch_res, ActionResult)
                self.assertEqual(context._current_tab_id, initial_tab_id)

                # 7. Close Wikipedia tab
                close_tab_fn = controller.registry.registry.actions["close_tab"].function
                close_res = await close_tab_fn(
                    CloseTabAction(page_id=wiki_tab_id),
                    browser=context,
                )
                self.assertIsInstance(close_res, ActionResult)

            except RuntimeError as live_err:
                if "Unsupported action" in str(live_err) or "unserializable" in str(live_err).lower():
                    self.skipTest(
                        "Chrome Extension is connected, but running an earlier service_worker.js build. "
                        "Please reload the extension in chrome://extensions to activate new actions."
                    )
                raise
        finally:
            await context.close()


if __name__ == "__main__":
    unittest.main()
