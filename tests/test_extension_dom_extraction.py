"""
Automated test suite for Milestone 1: Browser Use DOM Extraction Bridge.

Tests cover:
- Raw DOM map validation (rootId, map)
- DOMElementNode reconstruction using DomService._construct_dom_tree
- Parent-child tree consistency
- selector_map population and index resolution
- clickable_elements_to_string() execution
- BrowserState metadata population (url, title, tab_id in TabInfo, screenshot=None)
- Error handling for malformed DOM payloads
- Click-by-index XPath resolution
- Live Chrome Extension integration test (gracefully skipped if extension is not connected or not yet reloaded)
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any

from browser_use.browser.views import BrowserState, TabInfo
from browser_use.dom.views import DOMElementNode
from src.browser.dom_reconstruction import reconstruct_browser_state
from src.server.security import AUTH_TOKEN_ENV_VAR, SESSION_AUTH_TOKEN
from tests.local_fixture_server import LocalFixtureServer


def get_sample_fixture() -> dict[str, Any]:
    """Loads the deterministic sample DOM fixture generated from sample_page.html."""
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    if not fixture_path.exists():
        raise FileNotFoundError(f"Sample DOM fixture not found at {fixture_path}")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestDOMReconstructionUnit(unittest.TestCase):
    """Unit tests validating DOM reconstruction and BrowserState modeling without requiring a live browser."""

    def setUp(self):
        self.raw_data = get_sample_fixture()

    def test_raw_dom_map_has_valid_root_id(self):
        """Verify that the raw extraction output contains a valid rootId and node map."""
        eval_page = self.raw_data.get("eval_page", {})
        root_id = eval_page.get("rootId")
        node_map = eval_page.get("map", {})

        self.assertIsNotNone(root_id, "rootId must not be None")
        self.assertIn(str(root_id), node_map, f"rootId '{root_id}' must exist in node map")
        self.assertGreater(len(node_map), 0, "Node map must contain parsed nodes")

    def test_dom_element_node_reconstruction_succeeds(self):
        """Verify that reconstruct_browser_state creates a valid BrowserState with root DOMElementNode."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))

        self.assertIsInstance(state, BrowserState)
        self.assertIsInstance(state.element_tree, DOMElementNode)
        self.assertIsInstance(state.selector_map, dict)

    def test_parent_child_relationships_are_consistent(self):
        """Verify that reconstructed DOM nodes maintain reciprocal parent-child pointers."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))
        root = state.element_tree

        self.assertIsNone(root.parent, "Root node parent should be None")

        def walk_and_verify(node: DOMElementNode):
            for child in node.children:
                self.assertIs(
                    child.parent,
                    node,
                    f"Child {getattr(child, 'tag_name', 'TEXT')} parent pointer must match enclosing parent {node.tag_name}"
                )
                if isinstance(child, DOMElementNode):
                    walk_and_verify(child)

        walk_and_verify(root)

    def test_selector_map_contains_clickable_elements(self):
        """Verify that selector_map indexes the interactive elements on the test page."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))

        self.assertGreater(len(state.selector_map), 0, "selector_map must contain indexed interactive elements")

        tags_in_map = [node.tag_name.lower() for node in state.selector_map.values()]
        self.assertIn("input", tags_in_map, "selector_map should contain interactive input")
        self.assertIn("button", tags_in_map, "selector_map should contain interactive button")
        self.assertIn("a", tags_in_map, "selector_map should contain interactive anchor")

    def test_element_indices_resolve_to_dom_nodes(self):
        """Verify that every index in selector_map corresponds exactly to the node's highlight_index."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))

        for idx, node in state.selector_map.items():
            self.assertIsInstance(idx, int)
            self.assertIsInstance(node, DOMElementNode)
            self.assertEqual(node.highlight_index, idx)
            self.assertTrue(bool(node.xpath), f"Indexed element {idx} must have an XPath")

    def test_clickable_elements_to_string_runs_without_errors(self):
        """Verify that Browser Use's prompt generation method runs cleanly on the reconstructed tree."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))

        elements_str = state.element_tree.clickable_elements_to_string()
        self.assertIsInstance(elements_str, str)
        self.assertIn("<button", elements_str, "Prompt elements string should list button")
        self.assertIn("<input", elements_str, "Prompt elements string should list input")

    def test_browser_state_metadata_populated_correctly(self):
        """Verify that metadata fields (url, title, tab_id, scroll metrics) are populated."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))

        self.assertEqual(state.url, self.raw_data["url"])
        self.assertEqual(state.title, "Browser Use DOM Test Page")
        self.assertEqual(len(state.tabs), 1)

        tab = state.tabs[0]
        self.assertIsInstance(tab, TabInfo)
        self.assertEqual(tab.page_id, 42, "TabInfo must preserve actual Chrome tab ID (42)")
        self.assertEqual(tab.url, self.raw_data["url"])
        self.assertEqual(state.pixels_above, 0)
        self.assertEqual(state.pixels_below, 0)

    def test_screenshot_is_none(self):
        """Verify that screenshot is None as required for Milestone 1."""
        state = asyncio.run(reconstruct_browser_state(self.raw_data))
        self.assertIsNone(state.screenshot, "screenshot must be None in Milestone 1")

    def test_malformed_dom_payload_raises_value_error(self):
        """Verify that invalid or corrupt DOM extraction payloads raise descriptive ValueErrors."""
        with self.assertRaises(ValueError):
            asyncio.run(reconstruct_browser_state({"url": "http://x", "eval_page": None}))

        with self.assertRaises(ValueError):
            asyncio.run(reconstruct_browser_state({"url": "http://x", "eval_page": {"rootId": 1}}))

        with self.assertRaises(TypeError):
            asyncio.run(reconstruct_browser_state("not-a-dict"))


class TestClickByIndexVerification(unittest.TestCase):
    """Verifies that Browser Use index lookup properly resolves to the target element's XPath."""

    def setUp(self):
        self.raw_data = get_sample_fixture()
        self.state = asyncio.run(reconstruct_browser_state(self.raw_data))

    def test_click_by_index_xpath_resolution(self):
        """Verify finding button by index from selector_map yields the correct button XPath."""
        button_indices = [
            idx for idx, node in self.state.selector_map.items()
            if node.tag_name == "button"
        ]
        self.assertTrue(len(button_indices) > 0, "Must find at least one button in selector_map")

        target_index = button_indices[0]
        target_node = self.state.selector_map[target_index]

        self.assertEqual(target_node.tag_name, "button")
        self.assertEqual(target_node.attributes.get("id"), "test-btn")
        self.assertTrue("button" in target_node.xpath.lower())

        click_payload = {
            "xpath": target_node.xpath
        }
        self.assertEqual(click_payload["xpath"], target_node.xpath)


class TestLiveExtensionConnection(unittest.TestCase):
    """
    Live test against running FastAPI server and connected Chrome Extension.
    Safely skips if server is down, extension is not connected, or extension needs reload.
    """

    async def _check_extension_health(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get("http://127.0.0.1:8765/health")
                if res.status_code == 200:
                    data = res.json()
                    return data.get("extension_connected", False)
        except Exception:
            return False
        return False

    def test_live_dom_extraction_and_click(self):
        """
        Executes end-to-end against live Chrome Extension if available.
        Skipped cleanly if extension is not connected or needs to be reloaded in chrome://extensions.
        """
        connected = asyncio.run(self._check_extension_health())
        if not connected:
            self.skipTest("Live Chrome Extension is not currently connected to FastAPI on ws://127.0.0.1:8765 - skipping live test")

        token = os.getenv(AUTH_TOKEN_ENV_VAR) or SESSION_AUTH_TOKEN
        headers = {"Authorization": f"Bearer {token}"}

        server = LocalFixtureServer()
        server_url = server.start()

        async def run_live_test():
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 1. Create a dedicated agent-owned tab
                create_res = await client.post(
                    "http://127.0.0.1:8765/test/action",
                    headers=headers,
                    json={"action": "CREATE_NEW_TAB", "params": {"url": "about:blank", "active": False}}
                )
                self.assertEqual(create_res.status_code, 200, f"CREATE_NEW_TAB request failed: {create_res.text}")
                create_data = create_res.json()
                self.assertTrue(create_data.get("success"), f"Failed to create new tab: {create_data}")
                tab_id = create_data.get("result", {}).get("tabId")
                self.assertIsNotNone(tab_id, "CREATE_NEW_TAB did not return a valid tabId")

                try:
                    # 2. Navigate to local fixture
                    nav_res = await client.post(
                        "http://127.0.0.1:8765/test/action",
                        headers=headers,
                        json={"action": "NAVIGATE", "params": {"url": server_url, "tabId": tab_id}}
                    )
                    self.assertEqual(nav_res.status_code, 200, f"NAVIGATE failed: {nav_res.text}")
                    self.assertTrue(nav_res.json().get("success"), f"NAVIGATE returned error: {nav_res.json()}")

                    # 3. Extract DOM state from live extension
                    res = await client.post(
                        "http://127.0.0.1:8765/test/action",
                        headers=headers,
                        json={"action": "GET_DOM_STATE", "params": {"tabId": tab_id}}
                    )
                    self.assertEqual(res.status_code, 200, f"GET_DOM_STATE request failed: {res.text}")
                    action_data = res.json()

                    # If the connected extension is running an older service worker, inform tester
                    if not action_data.get("success") and "Unsupported action: GET_DOM_STATE" in str(action_data.get("error")):
                        return "EXTENSION_NEEDS_RELOAD"

                    self.assertTrue(action_data.get("success"), f"Extension returned failure: {action_data}")
                    raw_result = action_data.get("result", {})
                    self.assertIn("eval_page", raw_result)

                    # 4. Reconstruct BrowserState
                    state = await reconstruct_browser_state(raw_result)
                    self.assertIsInstance(state, BrowserState)
                    self.assertEqual(state.title, "Fixture Initial")
                    self.assertGreater(len(state.selector_map), 0, "No clickable elements found on fixture tab")

                    # 5. Find the button through returned selector/XPath information
                    button_nodes = [node for node in state.selector_map.values() if node.tag_name.lower() == "button"]
                    self.assertTrue(len(button_nodes) > 0, "Button element not found in selector map")
                    target_node = button_nodes[0]

                    # 6. Dispatch click via XPath
                    click_res = await client.post(
                        "http://127.0.0.1:8765/test/action",
                        headers=headers,
                        json={"action": "CLICK", "params": {"tabId": tab_id, "xpath": target_node.xpath}}
                    )
                    self.assertEqual(click_res.status_code, 200)
                    click_data = click_res.json()
                    self.assertTrue(click_data.get("success"), f"Click failed on xpath {target_node.xpath}: {click_data}")

                    # 7. Verify page title changes to 'clicked' by retrieving fresh browser state
                    fresh_res = await client.post(
                        "http://127.0.0.1:8765/test/action",
                        headers=headers,
                        json={"action": "GET_DOM_STATE", "params": {"tabId": tab_id}}
                    )
                    self.assertEqual(fresh_res.status_code, 200)
                    fresh_state = await reconstruct_browser_state(fresh_res.json().get("result", {}))
                    self.assertEqual(fresh_state.title, "clicked", f"Expected title 'clicked', got '{fresh_state.title}'")

                    return "SUCCESS"
                finally:
                    # Clean up agent-owned tab
                    try:
                        await client.post(
                            "http://127.0.0.1:8765/test/action",
                            headers=headers,
                            json={"action": "CLOSE_TAB", "params": {"tabId": tab_id}}
                        )
                    except Exception:
                        pass

        try:
            result = asyncio.run(run_live_test())
            if result == "EXTENSION_NEEDS_RELOAD":
                self.skipTest(
                    "Chrome Extension is connected, but running an earlier service_worker.js build. "
                    "Please reload the extension in chrome://extensions to activate the new GET_DOM_STATE action."
                )
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
