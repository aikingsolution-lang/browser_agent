"""
Comprehensive Regression Test Suite for Milestone 3B Remediation.

Validates all 11 specific requirements mandated by Stage 6:
1. Extension WebSocket URL contains no credentials.
2. Handshake succeeds with valid AUTH message.
3. Handshake fails and closes connection on invalid or missing AUTH.
4. Exact extension Origin validation succeeds for configured ID and fails closed when unconfigured.
5. New WebSocket connection cannot replace active extension without completing handshake.
6. Ownership persists across service worker restart using chrome.storage.session.
7. Empty ownership set authorizes zero tabs.
8. Initial get_state() with no active tab bootstraps a new tab instead of crashing.
9. Initial DOM extraction on about:blank produces valid BrowserState.
10. Click navigation handles delayed navigation up to 400 ms.
11. upload_file returns human-assistance message instead of crashing.

Plus additional regression safety tests:
12. Unauthorized HTTP RPC requests return 401.
13. Target tab closed returns explicit error (no silent fallback).
14. Missing target tab ID returns safe error.
15. Closing final agent-owned tab clears current tab safely.
16. Correct current tab ID prioritization with multiple open tabs (DEF-02).
17. Content extraction executes via extension without Playwright.
18. Keyboard Enter submission via send_keys (DEF-07).
19. Consecutive actions in multi_act sequence invalidate state correctly.
20. Zero Playwright: agent step succeeds with playwright.async_api poisoned.
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from browser_use.agent.views import ActionResult, AgentStepInfo
from browser_use.browser.views import BrowserState, TabInfo
from browser_use.controller.views import (
    ClickElementAction,
    CloseTabAction,
    GoToUrlAction,
    InputTextAction,
    SendKeysAction,
)
from browser_use.dom.views import DOMElementNode
from langchain_core.language_models.chat_models import BaseChatModel

from src.browser.dom_reconstruction import reconstruct_browser_state
from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController
from src.server.app import app
from src.server.security import (
    EXPECTED_EXTENSION_ID_ENV_VAR,
    SESSION_AUTH_TOKEN,
    validate_extension_origin,
)


def get_sample_fixture() -> dict[str, Any]:
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestWebSocketAuthAndSecurity(unittest.TestCase):
    """Stage 6 Requirements 1-5: WebSocket Authentication and Origin Security."""

    def setUp(self):
        self.client = TestClient(app)
        self.original_expected_id = os.environ.get(EXPECTED_EXTENSION_ID_ENV_VAR)

    def tearDown(self):
        if self.original_expected_id is not None:
            os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = self.original_expected_id
        else:
            os.environ.pop(EXPECTED_EXTENSION_ID_ENV_VAR, None)

    def test_01_extension_websocket_url_contains_no_credentials(self):
        """1. Extension WebSocket URL contains no credentials."""
        # A. Verify service_worker.js connects to ws://127.0.0.1:8765/ws/extension without query params
        sw_path = Path("src/chrome_extension/background/service_worker.js")
        sw_code = sw_path.read_text(encoding="utf-8")
        self.assertIn('const BACKEND_URL = "ws://127.0.0.1:8765/ws/extension";', sw_code)
        self.assertIn("const currentSocket = new WebSocket(BACKEND_URL);", sw_code)
        self.assertNotIn("?token=", sw_code)

        # B. Verify backend accepts raw WebSocket connection without query parameters
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = "testext01"
        with self.client.websocket_connect(
            "/ws/extension",
            headers={"Origin": "chrome-extension://testext01"},
        ) as ws:
            # Transport accepted without credentials in URL
            ws.send_json({"type": "AUTH", "token": SESSION_AUTH_TOKEN})
            welcome = ws.receive_json()
            self.assertEqual(welcome.get("type"), "WELCOME")
            self.assertTrue(welcome.get("authenticated"))

    def test_02_handshake_succeeds_with_valid_auth_message(self):
        """2. Handshake succeeds with valid AUTH message."""
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = "testext02"
        with self.client.websocket_connect(
            "/ws/extension",
            headers={"Origin": "chrome-extension://testext02"},
        ) as ws:
            ws.send_json({"type": "AUTH", "token": SESSION_AUTH_TOKEN})
            welcome = ws.receive_json()
            self.assertEqual(welcome["type"], "WELCOME")
            self.assertTrue(welcome["authenticated"])

    def test_03_handshake_fails_and_closes_connection_on_invalid_or_missing_auth(self):
        """3. Handshake fails and closes connection on invalid or missing AUTH."""
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = "testext03"

        # Case A: Invalid token
        with self.assertRaises(WebSocketDisconnect) as cm_bad_token:
            with self.client.websocket_connect(
                "/ws/extension",
                headers={"Origin": "chrome-extension://testext03"},
            ) as ws:
                ws.send_json({"type": "AUTH", "token": "invalid_wrong_token_value"})
                ws.receive_json()
        self.assertEqual(cm_bad_token.exception.code, 1008)

        # Case B: First message is not AUTH (e.g. HELLO)
        with self.assertRaises(WebSocketDisconnect) as cm_wrong_type:
            with self.client.websocket_connect(
                "/ws/extension",
                headers={"Origin": "chrome-extension://testext03"},
            ) as ws:
                ws.send_json({"type": "HELLO", "message": "Hi"})
                ws.receive_json()
        self.assertEqual(cm_wrong_type.exception.code, 1008)

    def test_04_exact_extension_origin_validation(self):
        """4. Exact extension Origin validation succeeds for configured ID and fails closed when unconfigured."""
        # Configured ID
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = "validextensionid123"

        # Valid Origin succeeds
        self.assertTrue(validate_extension_origin("chrome-extension://validextensionid123"))

        # Rogue extension ID fails
        self.assertFalse(validate_extension_origin("chrome-extension://rogueextensionid999"))

        # Malicious web origin fails
        self.assertFalse(validate_extension_origin("https://evil.com"))

        # When unconfigured, fails closed
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = ""
        self.assertFalse(
            validate_extension_origin("chrome-extension://validextensionid123"),
            "Must fail closed when EXPECTED_EXTENSION_ID is unconfigured",
        )

        # In TestClient: Unconfigured origin closes with code 1008
        with self.assertRaises(WebSocketDisconnect) as cm_unconfigured:
            with self.client.websocket_connect(
                "/ws/extension",
                headers={"Origin": "chrome-extension://validextensionid123"},
            ):
                pass
        self.assertEqual(cm_unconfigured.exception.code, 1008)

    def test_05_new_ws_cannot_replace_active_extension_without_handshake(self):
        """5. New WebSocket connection cannot replace active extension without completing handshake."""
        os.environ[EXPECTED_EXTENSION_ID_ENV_VAR] = "testext05"

        with self.client.websocket_connect(
            "/ws/extension",
            headers={"Origin": "chrome-extension://testext05"},
        ) as ws1:
            ws1.send_json({"type": "AUTH", "token": SESSION_AUTH_TOKEN})
            welcome = ws1.receive_json()
            self.assertEqual(welcome["type"], "WELCOME")

            # Second socket attempts connection while first is active
            with self.assertRaises(WebSocketDisconnect) as cm_conflict:
                with self.client.websocket_connect(
                    "/ws/extension",
                    headers={"Origin": "chrome-extension://testext05"},
                ):
                    pass
            self.assertEqual(cm_conflict.exception.code, 1008)

            # Original socket remains active and functional
            ws1.send_json({"type": "HEARTBEAT"})


class TestTabIsolationAndOwnership(unittest.IsolatedAsyncioTestCase):
    """Stage 6 Requirements 6-9: Tab Persistence, Strict Ownership, and Bootstrap."""

    def setUp(self):
        self.sample_data = get_sample_fixture()

    def test_06_tab_ownership_persists_in_storage_session(self):
        """6. Ownership persists across service worker restart using chrome.storage.session."""
        # Verify manifest.json includes "storage" permission
        manifest_path = Path("src/chrome_extension/manifest.json")
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("storage", manifest_data.get("permissions", []))

        # Verify service_worker.js implements session storage persistence
        sw_path = Path("src/chrome_extension/background/service_worker.js")
        sw_code = sw_path.read_text(encoding="utf-8")
        self.assertIn("chrome.storage.session.get", sw_code)
        self.assertIn("chrome.storage.session.set", sw_code)
        self.assertIn("getStoredAgentTabIds", sw_code)
        self.assertIn("addAgentTabId", sw_code)
        self.assertIn("removeAgentTabId", sw_code)
        self.assertIn("chrome.tabs.onRemoved.addListener", sw_code)

        # Verify in Python context that agent_tab_ids persists across state invalidations
        context = ExtensionBrowserContext()
        context.adopt_tab(777)
        self.assertIn(777, context.agent_tab_ids)
        context.invalidate_state()
        self.assertIn(777, context.agent_tab_ids, "Ownership must persist across state invalidation")

    async def test_07_empty_ownership_set_authorizes_zero_tabs(self):
        """7. Empty ownership set authorizes zero tabs (no wildcard leak)."""
        # A. Verify service_worker.js contains NO empty-set wildcard fallbacks
        sw_path = Path("src/chrome_extension/background/service_worker.js")
        sw_code = sw_path.read_text(encoding="utf-8")
        self.assertNotIn("agentTabIds.size === 0 ||", sw_code)
        self.assertNotIn("ownedIds.size === 0 ||", sw_code)

        # B. Verify in Python context: when no tabs are adopted, state.tabs contains zero tabs
        context = ExtensionBrowserContext()
        context.agent_tab_ids.clear()
        context._current_tab_id = None

        mock_payload = {
            "url": "about:blank",
            "title": "",
            "tab_id": 999,
            "eval_page": {
                "rootId": "0",
                "map": {
                    "0": {
                        "tagName": "body",
                        "xpath": "/html/body",
                        "attributes": {},
                        "children": [],
                        "isVisible": True,
                    }
                },
            },
            "pixels_above": 0,
            "pixels_below": 0,
            # Chrome has human tabs open
            "all_tabs": [
                {"tabId": 101, "url": "https://bank.com", "title": "Bank", "active": False},
                {"tabId": 102, "url": "https://email.com", "title": "Email", "active": True},
            ],
        }

        # Mock CREATE_NEW_TAB to return tabId 999
        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            def side_effect(action, params=None):
                if action == "CREATE_NEW_TAB":
                    return {"type": "ACTION_RESULT", "action": "CREATE_NEW_TAB", "success": True, "result": {"tabId": 999}}
                elif action == "GET_DOM_STATE":
                    return {"type": "ACTION_RESULT", "action": "GET_DOM_STATE", "success": True, "result": mock_payload}
                return {"type": "ACTION_RESULT", "success": True, "result": {}}

            mock_exec.side_effect = side_effect
            state = await context.get_state()

            # State.tabs must only contain the agent-owned tab (999), zero human tabs
            tab_ids = [t.page_id for t in state.tabs]
            self.assertEqual(tab_ids, [999])
            self.assertNotIn(101, tab_ids)
            self.assertNotIn(102, tab_ids)

    async def test_08_initial_get_state_bootstraps_new_tab(self):
        """8. Initial get_state() with no active tab bootstraps a new tab instead of crashing."""
        context = ExtensionBrowserContext()
        self.assertIsNone(context._current_tab_id)
        self.assertEqual(len(context.agent_tab_ids), 0)

        called_actions = []

        async def mock_execute(action, params=None):
            called_actions.append((action, params))
            if action == "CREATE_NEW_TAB":
                return {
                    "type": "ACTION_RESULT",
                    "action": "CREATE_NEW_TAB",
                    "success": True,
                    "result": {"tabId": 404, "url": "about:blank"},
                }
            elif action == "GET_DOM_STATE":
                return {
                    "type": "ACTION_RESULT",
                    "action": "GET_DOM_STATE",
                    "success": True,
                    "result": {
                        "url": "about:blank",
                        "title": "",
                        "tab_id": 404,
                        "eval_page": {
                            "rootId": "0",
                            "map": {
                                "0": {
                                    "tagName": "body",
                                    "xpath": "/html/body",
                                    "attributes": {},
                                    "children": [],
                                    "isVisible": True,
                                }
                            },
                        },
                        "pixels_above": 0,
                        "pixels_below": 0,
                        "all_tabs": [{"tabId": 404, "url": "about:blank", "title": "", "active": False}],
                    },
                }
            return {"type": "ACTION_RESULT", "success": True, "result": {}}

        with patch.object(context, "execute_action", side_effect=mock_execute):
            state = await context.get_state()

            # Verify bootstrap sequence: CREATE_NEW_TAB called first, then GET_DOM_STATE
            self.assertEqual(len(called_actions), 2)
            self.assertEqual(called_actions[0][0], "CREATE_NEW_TAB")
            self.assertEqual(called_actions[0][1], {"url": "about:blank", "active": False})
            self.assertEqual(called_actions[1][0], "GET_DOM_STATE")
            self.assertEqual(called_actions[1][1], {"tabId": 404})

            # Verify tab ownership adopted
            self.assertEqual(context._current_tab_id, 404)
            self.assertIn(404, context.agent_tab_ids)
            self.assertEqual(state.url, "about:blank")

    async def test_09_initial_dom_extraction_on_about_blank_produces_valid_browser_state(self):
        """9. Initial DOM extraction on about:blank produces valid BrowserState."""
        # A. Test reconstruct_browser_state on empty about:blank payload
        about_blank_payload = {
            "url": "about:blank",
            "title": "",
            "tab_id": 300,
            "eval_page": {
                "rootId": "0",
                "map": {
                    "0": {
                        "tagName": "body",
                        "xpath": "/html/body",
                        "attributes": {},
                        "children": [],
                        "isVisible": True,
                    }
                },
            },
            "pixels_above": 0,
            "pixels_below": 0,
            "all_tabs": [{"tabId": 300, "url": "about:blank", "title": "", "active": False}],
        }

        state = await reconstruct_browser_state(about_blank_payload)
        self.assertIsInstance(state, BrowserState)
        self.assertIsInstance(state.element_tree, DOMElementNode)
        self.assertEqual(state.element_tree.tag_name, "body")
        self.assertEqual(state.selector_map, {})
        self.assertEqual(state.url, "about:blank")
        self.assertEqual(state.title, "")
        self.assertEqual(len(state.tabs), 1)
        self.assertEqual(state.tabs[0].page_id, 300)

        # B. Verify service_worker.js contains dedicated handling for targetTab.url === 'about:blank'
        sw_path = Path("src/chrome_extension/background/service_worker.js")
        sw_code = sw_path.read_text(encoding="utf-8")
        self.assertIn('targetTab.url === "about:blank"', sw_code)


class TestNavigationTimingAndUploadRemediation(unittest.IsolatedAsyncioTestCase):
    """Stage 6 Requirements 10-11: Navigation Timing and Upload Action."""

    async def test_10_click_navigation_handles_delayed_navigation_up_to_400ms(self):
        """10. Click navigation handles delayed navigation up to 400 ms."""
        # A. Verify service_worker.js click grace period is >= 450ms
        sw_path = Path("src/chrome_extension/background/service_worker.js")
        sw_code = sw_path.read_text(encoding="utf-8")
        self.assertIn("setTimeout(r, 450)", sw_code)
        self.assertIn("postWaitTab.status === \"loading\"", sw_code)

        # B. Verify service_worker.js catches "Execution context was destroyed" and retries once
        self.assertIn("Execution context was destroyed", sw_code)
        self.assertIn("waitForTabLoad(tabId, 15000)", sw_code)

        # C. Verify click_element_by_index invalidates state so next state retrieval is clean
        context = ExtensionBrowserContext()
        context.adopt_tab(42)

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "CLICK",
                "success": True,
                "result": {"success": True, "tag": "BUTTON", "text": "Navigate"},
            }

            with patch.object(context, "get_selector_map", new_callable=AsyncMock) as mock_map:
                mock_node = MagicMock()
                mock_node.xpath = "/html/body/button"
                mock_node.tag_name = "BUTTON"
                mock_node.get_all_text_till_next_clickable_element.return_value = "Navigate"
                mock_map.return_value = {0: mock_node}

                controller = ExtensionController()
                click_fn = controller.registry.registry.actions["click_element_by_index"].function

                res = await click_fn(ClickElementAction(index=0), browser=context)
                self.assertIn("Clicked element with index 0", res.extracted_content)
                self.assertIsNone(context._state, "State must be invalidated after click")

    async def test_11_upload_file_returns_human_assistance_message(self):
        """11. upload_file returns human-assistance message instead of crashing."""
        context = ExtensionBrowserContext()
        context.adopt_tab(42)

        controller = ExtensionController()
        upload_fn = controller.registry.registry.actions["upload_file"].function

        # Execute upload_file with arbitrary path
        res = await upload_fn(
            index=3,
            path="C:/documents/report.pdf",
            browser=context,
        )

        self.assertIsInstance(res, ActionResult)
        self.assertIsNotNone(res.error)
        self.assertIn("restricted by Chrome Extension security policies", res.error)
        self.assertIn("ask_for_assistant", res.error)

        # Confirm ask_for_assistant is available and preserved
        self.assertIn("ask_for_assistant", controller.registry.registry.actions)
        ask_fn = controller.registry.registry.actions["ask_for_assistant"].function
        ask_res = await ask_fn(query="Please select the file", browser=context)
        self.assertIsInstance(ask_res, ActionResult)


class TestAdditionalRegressionSafety(unittest.IsolatedAsyncioTestCase):
    """Safety and Robustness Tests 12-20."""

    def setUp(self):
        self.sample_data = get_sample_fixture()

    def test_12_http_rpc_requires_bearer_auth(self):
        """12. POST /api/v1/action without valid Bearer token returns 401."""
        client = TestClient(app)
        res1 = client.post("/api/v1/action", json={"action": "GET_TABS"})
        self.assertEqual(res1.status_code, 401)

        res2 = client.post(
            "/api/v1/action",
            json={"action": "GET_TABS"},
            headers={"Authorization": "Bearer invalid_secret_token"},
        )
        self.assertEqual(res2.status_code, 401)

    async def test_13_target_tab_closed_returns_explicit_error(self):
        """13. Closed target tab returns descriptive error without silent fallback to active tab."""
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

    async def test_14_missing_target_tab_id_returns_safe_error(self):
        """14. Missing target tab ID (None) returns safe error without active tab fallback."""
        context = ExtensionBrowserContext()
        context._current_tab_id = None

        controller = ExtensionController()
        action_fn = controller.registry.registry.actions["click_element_by_index"].function
        res = await action_fn(ClickElementAction(index=0), browser=context)

        self.assertIsInstance(res, ActionResult)
        self.assertIn("No agent-owned browser tabs remain open", res.error)

    async def test_15_closing_final_agent_owned_tab(self):
        """15. Closing final agent-owned tab clears current tab safely."""
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
            self.assertIsNone(context._current_tab_id)
            self.assertEqual(len(context.agent_tab_ids), 0)

            # Subsequent action returns safe error
            type_fn = controller.registry.registry.actions["input_text"].function
            next_res = await type_fn(InputTextAction(index=0, text="hello"), browser=context)
            self.assertIn("No agent-owned browser tabs remain open", next_res.error)

    async def test_16_correct_current_tab_id_with_multiple_open_tabs(self):
        """16. DEF-02 regression: get_state() prioritizes inspected tab_id over state.tabs[0]."""
        context = ExtensionBrowserContext()
        context.adopt_tab(888)

        mock_payload = {
            "url": "https://target-site.com",
            "title": "Target Tab",
            "tab_id": 888,
            "eval_page": self.sample_data["eval_page"],
            "pixels_above": 0,
            "pixels_below": 0,
            "all_tabs": [
                {"tabId": 111, "url": "https://other.com", "title": "Other", "active": False},
                {"tabId": 888, "url": "https://target-site.com", "title": "Target Tab", "active": True},
            ],
        }

        with patch.object(context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"type": "ACTION_RESULT", "action": "GET_DOM_STATE", "success": True, "result": mock_payload}
            state = await context.get_state()
            self.assertEqual(context._current_tab_id, 888)

    async def test_17_extract_content_execution(self):
        """17. extract_content executes via extension without Playwright get_current_page crashes."""
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

    async def test_18_send_keys_enter_action(self):
        """18. send_keys action dispatches PRESS_KEY with Enter parameter cleanly."""
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

    async def test_19_consecutive_actions_multi_act_sequence(self):
        """19. Three consecutive actions (click -> type -> click) execute cleanly with cache invalidation."""
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

    async def test_20_agent_operates_with_poisoned_playwright(self):
        """20. Agent step runs to completion with playwright.async_api.async_playwright poisoned."""
        import playwright.async_api

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
                    step_info = AgentStepInfo(step_number=0, max_steps=1)
                    await agent.step(step_info)
                    self.assertTrue(agent.state.history.is_done())

            await context.close()
            await browser.close()
        finally:
            playwright.async_api.async_playwright = original_playwright


if __name__ == "__main__":
    unittest.main()
