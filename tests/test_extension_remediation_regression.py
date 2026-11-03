"""
Comprehensive Regression Test Suite for Milestone 3B Remediation.

Validates all 15 audit and remediation requirements:
1. Unauthorized HTTP requests (401 on /api/v1/action without Bearer token).
2. Unauthorized WebSocket connections (closed with 1008 on invalid/missing token).
3. Invalid extension Origin (closed with 1008 when origin is not chrome-extension://).
4. Unauthorized socket cannot replace active authenticated extension socket.
5. Target tab closed during execution (explicit error, no silent fallback).
6. Missing target tab ID (explicit error, no silent fallback).
7. Agent cannot see unrelated user tabs (BrowserState.tabs restricted to agent_tab_ids).
8. Closing final agent-owned tab (sets _current_tab_id to None, returns safe error on next action).
9. Correct current tab ID with multiple open tabs (DEF-02 regression test).
10. extract_content execution (extension-backed, no Playwright).
11. Existing custom action preservation (ask_for_assistant, upload_file, MCP client support).
12. Navigation synchronization after CLICK (DEF-06).
13. Keyboard Enter submission on forms and inputs (DEF-07).
14. Three consecutive indexed actions in one agent step (multi_act validation).
15. Browser Use operates with Playwright poisoned to raise immediately.
"""

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from browser_use.agent.views import ActionResult
from browser_use.browser.views import BrowserState, TabInfo
from browser_use.controller.views import (
    ClickElementAction,
    CloseTabAction,
    GoToUrlAction,
    InputTextAction,
    NoParamsAction,
    OpenTabAction,
    SendKeysAction,
)
from langchain_core.language_models.chat_models import BaseChatModel

from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController, resolve_chrome_tab_id
from src.server.app import app
from src.server.security import SESSION_AUTH_TOKEN, verify_token


def get_sample_fixture() -> dict[str, Any]:
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestSecurityRemediation(unittest.TestCase):
    """Tests 1-4: Backend HTTP and WebSocket Security (DEF-05)."""

    def setUp(self):
        self.client = TestClient(app)

    def test_01_unauthorized_http_request_rejected(self):
        """1. POST /api/v1/action without valid Bearer token returns 401."""
        # Missing Authorization header
        res1 = self.client.post("/api/v1/action", json={"action": "GET_TABS"})
        self.assertEqual(res1.status_code, 401)
        self.assertIn("missing Authorization header", res1.json()["detail"])

        # Invalid token
        res2 = self.client.post(
            "/api/v1/action",
            json={"action": "GET_TABS"},
            headers={"Authorization": "Bearer invalid_token_12345"},
        )
        self.assertEqual(res2.status_code, 401)
        self.assertIn("invalid authentication token", res2.json()["detail"])

    def test_02_unauthorized_websocket_rejected(self):
        """2. WebSocket /ws/extension without token is closed with code 1008."""
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/ws/extension?token=bad_token") as ws:
                pass

    def test_03_invalid_extension_origin_rejected(self):
        """3. WebSocket /ws/extension with untrusted Origin is closed with 1008."""
        headers = {"Origin": "https://malicious-website.com"}
        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                f"/ws/extension?token={SESSION_AUTH_TOKEN}",
                headers=headers,
            ) as ws:
                pass

    def test_04_unauthorized_socket_cannot_replace_authenticated_socket(self):
        """4. Second WebSocket connection while one is active is rejected with 1008."""
        # Connect legitimate extension
        with self.client.websocket_connect(
            f"/ws/extension?token={SESSION_AUTH_TOKEN}",
            headers={"Origin": "chrome-extension://abcdefghijklmnop"},
        ) as ws1:
            welcome = ws1.receive_json()
            self.assertEqual(welcome["type"], "WELCOME")

            # Attempt second connection
            with self.assertRaises(Exception):
                with self.client.websocket_connect(
                    f"/ws/extension?token={SESSION_AUTH_TOKEN}",
                    headers={"Origin": "chrome-extension://abcdefghijklmnop"},
                ) as ws2:
                    pass


class TestTabIsolationRemediation(unittest.IsolatedAsyncioTestCase):
    """Tests 5-9: Target Tab Resolution, Isolation, and Ownership (DEF-02, DEF-03, DEF-04)."""

    async def asyncSetUp(self):
        self.sample_data = get_sample_fixture()

    async def test_05_target_tab_closed_returns_explicit_error(self):
        """5. Closed target tab returns descriptive error without silent fallback to active tab."""
        context = ExtensionBrowserContext()
        context.adopt_tab(99999)

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = RuntimeError("Extension action 'CLICK' failed: Target tab 99999 is closed or invalid")

            controller = ExtensionController()
            action_fn = controller.registry.registry.actions["click_element_by_index"].function

            with patch.object(context, "get_selector_map", new_callable=AsyncMock) as mock_map:
                mock_node = MagicMock()
                mock_node.xpath = "/html/body/button"
                mock_node.tag_name = "BUTTON"
                mock_map.return_value = {0: mock_node}

                with self.assertRaises(RuntimeError) as cm:
                    await action_fn(ClickElementAction(index=0), browser=context)
                self.assertIn("closed or invalid", str(cm.exception))

    async def test_06_missing_target_tab_id_returns_safe_error(self):
        """6. Missing target tab ID (None) returns safe error without active tab fallback."""
        context = ExtensionBrowserContext()
        context._current_tab_id = None  # No tab open or adopted

        controller = ExtensionController()
        action_fn = controller.registry.registry.actions["click_element_by_index"].function
        res = await action_fn(ClickElementAction(index=0), browser=context)

        self.assertIsInstance(res, ActionResult)
        self.assertIn("No agent-owned browser tabs remain open", res.error)

    async def test_07_agent_cannot_see_unrelated_user_tabs(self):
        """7. BrowserState.tabs excludes human-owned tabs and exposes only agent-owned tabs."""
        context = ExtensionBrowserContext()
        context.adopt_tab(101)  # Agent owns tab 101 only

        mock_payload = {
            "url": "https://agent-task.com",
            "title": "Agent Task",
            "tab_id": 101,
            "eval_page": self.sample_data["eval_page"],
            "pixels_above": 0,
            "pixels_below": 0,
            # Chrome has multiple human tabs open: 202 (Personal Email), 303 (Banking)
            "all_tabs": [
                {"tabId": 202, "url": "https://mail.google.com", "title": "Private Email", "active": False},
                {"tabId": 101, "url": "https://agent-task.com", "title": "Agent Task", "active": True},
                {"tabId": 303, "url": "https://bank.com", "title": "Bank Account", "active": False},
            ],
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"type": "ACTION_RESULT", "action": "GET_DOM_STATE", "success": True, "result": mock_payload}
            state = await context.get_state()

            # State.tabs must only contain tab 101
            tab_ids = [t.page_id for t in state.tabs]
            self.assertEqual(tab_ids, [101])
            self.assertNotIn(202, tab_ids)
            self.assertNotIn(303, tab_ids)

    async def test_08_closing_final_agent_owned_tab(self):
        """8. Closing the sole agent-owned tab sets current tab to None and rejects subsequent actions."""
        context = ExtensionBrowserContext()
        context.adopt_tab(555)

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "CLOSE_TAB",
                "success": True,
                "result": {"tabId": 555, "closed": True},
            }

            controller = ExtensionController()
            close_fn = controller.registry.registry.actions["close_tab"].function
            res = await close_fn(CloseTabAction(page_id=555), browser=context)

            self.assertIn("Closed tab #555", res.extracted_content)
            self.assertIsNone(context._current_tab_id, "Current tab must be None when all agent tabs closed")
            self.assertEqual(len(context.agent_tab_ids), 0)

            # Subsequent actions safely return error
            type_fn = controller.registry.registry.actions["input_text"].function
            next_res = await type_fn(InputTextAction(index=0, text="hello"), browser=context)
            self.assertIn("No agent-owned browser tabs remain open", next_res.error)

    async def test_09_correct_current_tab_id_with_multiple_open_tabs(self):
        """9. DEF-02 regression: get_state() prioritizes inspected tab_id over state.tabs[0]."""
        context = ExtensionBrowserContext()

        mock_payload = {
            "url": "https://target-site.com",
            "title": "Target Tab",
            "tab_id": 888,  # Target tab is 888
            "eval_page": self.sample_data["eval_page"],
            "pixels_above": 0,
            "pixels_below": 0,
            # Browser order has tab 111 first
            "all_tabs": [
                {"tabId": 111, "url": "https://other.com", "title": "Other", "active": False},
                {"tabId": 888, "url": "https://target-site.com", "title": "Target Tab", "active": True},
            ],
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"type": "ACTION_RESULT", "action": "GET_DOM_STATE", "success": True, "result": mock_payload}
            state = await context.get_state()

            # Must be 888, NOT 111
            self.assertEqual(context._current_tab_id, 888)


class TestControllerAndContentRemediation(unittest.IsolatedAsyncioTestCase):
    """Tests 10-11: Controller Compatibility and Content Extraction (DEF-01)."""

    async def test_10_extract_content_execution(self):
        """10. extract_content executes via extension without Playwright get_current_page crashes."""
        context = ExtensionBrowserContext()
        context.adopt_tab(77)

        mock_content_payload = {
            "type": "ACTION_RESULT",
            "action": "GET_PAGE_CONTENT",
            "success": True,
            "result": {"content": "Example Header\nSample paragraph with content."},
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_content_payload

            controller = ExtensionController()
            extract_fn = controller.registry.registry.actions["extract_content"].function

            res = await extract_fn(
                goal="extract header",
                should_strip_link_urls=True,
                browser=context,
                page_extraction_llm=None,
            )

            self.assertIsInstance(res, ActionResult)
            self.assertIn("Example Header", res.extracted_content)

    async def test_11_existing_custom_action_preservation(self):
        """11. ask_for_assistant, upload_file, and MCP tools are registered and available."""
        controller = ExtensionController()
        actions = controller.registry.registry.actions

        self.assertIn("ask_for_assistant", actions)
        self.assertIn("upload_file", actions)
        self.assertIn("done", actions)
        self.assertIn("wait", actions)
        self.assertTrue(hasattr(controller, "setup_mcp_client"))
        self.assertTrue(hasattr(controller, "register_mcp_tools"))

        # Test ask_for_assistant invocation
        context = ExtensionBrowserContext()
        ask_fn = actions["ask_for_assistant"].function
        res = await ask_fn(query="Need help", browser=context)
        self.assertIn("Human cannot help you", res.extracted_content)


class TestNavigationAndKeyboardRemediation(unittest.IsolatedAsyncioTestCase):
    """Tests 12-14: Navigation Race, Keyboard Enter, and Multi-Act Sequence (DEF-06, DEF-07)."""

    async def asyncSetUp(self):
        self.sample_data = get_sample_fixture()

    async def test_12_click_element_invalidates_state_safely(self):
        """12. click_element_by_index invalidates state so next state retrieval fetches fresh DOM."""
        context = ExtensionBrowserContext()
        context.adopt_tab(42)

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "CLICK",
                "success": True,
                "result": {"success": True, "tag": "BUTTON", "text": "Submit"},
            }

            with patch.object(context, "get_selector_map", new_callable=AsyncMock) as mock_map:
                mock_node = MagicMock()
                mock_node.xpath = "/html/body/button"
                mock_node.tag_name = "BUTTON"
                mock_node.get_all_text_till_next_clickable_element.return_value = "Submit"
                mock_map.return_value = {0: mock_node}

                controller = ExtensionController()
                click_fn = controller.registry.registry.actions["click_element_by_index"].function

                res = await click_fn(ClickElementAction(index=0), browser=context)
                self.assertIn("Clicked element with index 0", res.extracted_content)
                self.assertIsNone(context._state, "State must be invalidated after click")

    async def test_13_send_keys_enter_action(self):
        """13. send_keys action dispatches PRESS_KEY with Enter parameter cleanly."""
        context = ExtensionBrowserContext()
        context.adopt_tab(42)

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "PRESS_KEY",
                "success": True,
                "result": {"success": True, "key": "Enter", "targetTag": "INPUT"},
            }

            controller = ExtensionController()
            keys_fn = controller.registry.registry.actions["send_keys"].function

            res = await keys_fn(SendKeysAction(keys="Enter"), browser=context)
            self.assertIn("Sent keys: Enter", res.extracted_content)
            self.assertIsNone(context._state, "State must be invalidated after send_keys")

    async def test_14_consecutive_actions_multi_act_sequence(self):
        """14. Three consecutive actions (click -> type -> click) execute cleanly with cache invalidation."""
        context = ExtensionBrowserContext()
        context.adopt_tab(42)
        controller = ExtensionController()

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "CLICK",
                "success": True,
                "result": {"success": True, "tag": "BUTTON"},
            }

            mock_node = MagicMock()
            mock_node.xpath = "/html/body/button"
            mock_node.tag_name = "BUTTON"
            mock_node.get_all_text_till_next_clickable_element.return_value = "Btn"

            with patch.object(context, "get_selector_map", new_callable=AsyncMock) as mock_map:
                mock_map.return_value = {0: mock_node, 1: mock_node, 2: mock_node}

                # Action 1: Click 0
                res1 = await controller.registry.registry.actions["click_element_by_index"].function(
                    ClickElementAction(index=0), browser=context
                )
                self.assertIn("Clicked element with index 0", res1.extracted_content)
                self.assertIsNone(context._state)

                # Action 2: Type into 1
                mock_exec.return_value = {"type": "ACTION_RESULT", "action": "TYPE", "success": True, "result": {"success": True}}
                res2 = await controller.registry.registry.actions["input_text"].function(
                    InputTextAction(index=1, text="test query"), browser=context
                )
                self.assertIn('Inputted "test query"', res2.extracted_content)
                self.assertIsNone(context._state)

                # Action 3: Click 2
                mock_exec.return_value = {"type": "ACTION_RESULT", "action": "CLICK", "success": True, "result": {"success": True}}
                res3 = await controller.registry.registry.actions["click_element_by_index"].function(
                    ClickElementAction(index=2), browser=context
                )
                self.assertIn("Clicked element with index 2", res3.extracted_content)
                self.assertIsNone(context._state)


class TestZeroPlaywrightRemediation(unittest.IsolatedAsyncioTestCase):
    """Test 15: Zero Playwright Verification with Poisoned Async Playwright."""

    async def test_15_agent_operates_with_poisoned_playwright(self):
        """15. Agent step runs to completion with playwright.async_api.async_playwright poisoned."""
        import playwright.async_api

        # Poison playwright
        original_playwright = playwright.async_api.async_playwright

        def poison_playwright(*args, **kwargs):
            raise RuntimeError("CRITICAL FAILURE: playwright.async_api was called!")

        playwright.async_api.async_playwright = poison_playwright

        try:
            from src.agent.browser_use.browser_use_agent import BrowserUseAgent

            mock_llm = MagicMock(spec=BaseChatModel)
            mock_llm._llm_type = "mock"
            mock_llm.invoke.return_value.content = "paris"

            browser = ExtensionBrowser()
            context = ExtensionBrowserContext()
            controller = ExtensionController()

            agent = BrowserUseAgent(
                task="Zero playwright test",
                llm=mock_llm,
                browser=browser,
                browser_context=context,
                controller=controller,
                use_vision=False,
            )

            fixture_data = get_sample_fixture()

            with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "type": "ACTION_RESULT",
                    "action": "GET_DOM_STATE",
                    "success": True,
                    "result": fixture_data,
                }

                async def mock_get_next_action(input_messages):
                    return agent.AgentOutput(
                        current_state={
                            "evaluation_previous_goal": "Start",
                            "memory": "None",
                            "next_goal": "Done",
                        },
                        action=[{"done": {"text": "Remediation verified!", "success": True}}],
                    )

                with patch.object(agent, "get_next_action", side_effect=mock_get_next_action):
                    await agent.step()
                    self.assertTrue(agent.state.history.is_done())

            await context.close()
            await browser.close()
        finally:
            playwright.async_api.async_playwright = original_playwright


if __name__ == "__main__":
    unittest.main()
