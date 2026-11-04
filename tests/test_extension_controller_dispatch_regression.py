"""
Regression test suite for ExtensionController action dispatch.

Tests that registered actions in ExtensionController can be dispatched through
the installed Browser Use registry mechanism (Registry.execute_action) without
raising:
    TypeError: issubclass() arg 1 must be a class
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from browser_use.agent.views import ActionResult
from browser_use.browser.views import BrowserState, TabInfo
from browser_use.dom.views import DOMElementNode
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController


class TestExtensionControllerActionDispatchRegression(unittest.IsolatedAsyncioTestCase):
    """Exercises real action dispatch through Controller.registry.execute_action."""

    def setUp(self):
        self.controller = ExtensionController()
        self.mock_browser = MagicMock(spec=ExtensionBrowserContext)
        self.mock_browser._current_tab_id = 1
        self.mock_browser.agent_tab_ids = {1}
        self.mock_browser.execute_action = AsyncMock(return_value={"success": True, "result": {}})
        self.mock_browser.invalidate_state = MagicMock()
        self.mock_browser.adopt_tab = MagicMock()

    async def test_01_dispatch_go_to_url(self):
        """Verify go_to_url dispatches via registry without issubclass TypeError."""
        result = await self.controller.registry.execute_action(
            "go_to_url",
            {"url": "https://example.com"},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)
        self.mock_browser.execute_action.assert_awaited_once_with(
            "NAVIGATE", {"url": "https://example.com", "tabId": 1}
        )

    async def test_02_dispatch_search_google(self):
        """Verify search_google dispatches via registry without issubclass TypeError."""
        result = await self.controller.registry.execute_action(
            "search_google",
            {"query": "antigravity browser"},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)
        self.mock_browser.execute_action.assert_awaited_once_with(
            "NAVIGATE",
            {"url": "https://www.google.com/search?q=antigravity browser&udm=14", "tabId": 1},
        )

    async def test_03_dispatch_open_tab(self):
        """Verify open_tab dispatches via registry without issubclass TypeError."""
        self.mock_browser.execute_action.return_value = {
            "success": True,
            "result": {"tabId": 10},
        }
        result = await self.controller.registry.execute_action(
            "open_tab",
            {"url": "https://example.org"},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)
        self.mock_browser.execute_action.assert_awaited_once_with(
            "CREATE_NEW_TAB", {"url": "https://example.org"}
        )

    async def test_04_dispatch_click_element_by_index(self):
        """Verify click_element_by_index dispatches via registry without issubclass TypeError."""
        mock_node = MagicMock(spec=DOMElementNode)
        mock_node.tag_name = "button"
        mock_node.xpath = "/html/body/button"
        self.mock_browser.get_dom_element_by_index = AsyncMock(return_value=mock_node)

        result = await self.controller.registry.execute_action(
            "click_element_by_index",
            {"index": 0},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)

    async def test_05_dispatch_input_text(self):
        """Verify input_text dispatches via registry without issubclass TypeError."""
        mock_node = MagicMock(spec=DOMElementNode)
        mock_node.tag_name = "input"
        mock_node.xpath = "/html/body/input"
        self.mock_browser.get_dom_element_by_index = AsyncMock(return_value=mock_node)

        result = await self.controller.registry.execute_action(
            "input_text",
            {"index": 1, "text": "test search"},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)

    async def test_06_dispatch_switch_tab(self):
        """Verify switch_tab dispatches via registry without issubclass TypeError."""
        self.mock_browser.agent_tab_ids = {1, 2}
        result = await self.controller.registry.execute_action(
            "switch_tab",
            {"page_id": 2},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)

    async def test_07_dispatch_close_tab(self):
        """Verify close_tab dispatches via registry without issubclass TypeError."""
        self.mock_browser.agent_tab_ids = {1}
        result = await self.controller.registry.execute_action(
            "close_tab",
            {"page_id": 1},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)

    async def test_08_dispatch_scroll_down_and_up(self):
        """Verify scroll_down and scroll_up dispatch via registry without issubclass TypeError."""
        down_res = await self.controller.registry.execute_action(
            "scroll_down",
            {"amount": 300},
            browser=self.mock_browser,
        )
        self.assertIsInstance(down_res, ActionResult)

        up_res = await self.controller.registry.execute_action(
            "scroll_up",
            {"amount": 300},
            browser=self.mock_browser,
        )
        self.assertIsInstance(up_res, ActionResult)

    async def test_09_dispatch_send_keys(self):
        """Verify send_keys dispatches via registry without issubclass TypeError."""
        result = await self.controller.registry.execute_action(
            "send_keys",
            {"keys": "Enter"},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)

    async def test_10_dispatch_go_back(self):
        """Verify go_back dispatches via registry without issubclass TypeError."""
        result = await self.controller.registry.execute_action(
            "go_back",
            {},
            browser=self.mock_browser,
        )
        self.assertIsInstance(result, ActionResult)


if __name__ == "__main__":
    unittest.main()
