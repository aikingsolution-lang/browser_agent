"""
Automated test suite for Milestone 3A: Agent Integration.

Tests cover:
1. ExtensionBrowser proxy properties, safe no-ops, and zero Playwright inheritance.
2. BrowserUseAgent automatic injection of ExtensionBrowser and ExtensionBrowserContext.
3. Proof that Chromium/Playwright is not launched during agent initialization.
4. Agent BrowserState retrieval via extension context.
5. Agent execution of a complete reasoning step (with DoneAction).
6. Clean Agent shutdown and resource cleanup.
7. Live agent integration test against running Chrome Extension (if connected).
"""

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from browser_use.agent.views import AgentStepInfo
from browser_use.browser.browser import Browser, BrowserConfig
from browser_use.browser.views import BrowserState
from browser_use.dom.views import DOMElementNode
from langchain_core.language_models.chat_models import BaseChatModel

from src.agent.browser_use.browser_use_agent import BrowserUseAgent
from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext, ExtensionTabProxy


def get_sample_fixture() -> dict[str, Any]:
    """Loads the deterministic sample DOM fixture generated in Milestone 1."""
    fixture_path = Path("assets/test_pages/sample_dom_fixture.json").resolve()
    if not fixture_path.exists():
        raise FileNotFoundError(f"Sample fixture not found at {fixture_path}")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


def create_mock_llm() -> MagicMock:
    """Creates a mock BaseChatModel satisfying Pydantic validation."""
    mock = MagicMock(spec=BaseChatModel)
    mock._llm_type = "mock"
    mock.invoke.return_value.content = "paris"
    return mock


class TestExtensionBrowserUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the lightweight ExtensionBrowser proxy."""

    async def test_extension_browser_attributes_and_defaults(self):
        """Verify ExtensionBrowser has config, playwright=None, and does not inherit from Browser."""
        browser = ExtensionBrowser()

        self.assertFalse(isinstance(browser, Browser), "ExtensionBrowser must NOT inherit from Browser")
        self.assertIsInstance(browser.config, BrowserConfig)
        self.assertIsNone(browser.playwright, "playwright attribute must be None")

        # Verify async no-ops
        await browser._init()
        await browser.close()

    async def test_extension_browser_new_context(self):
        """Verify new_context() returns a configured ExtensionBrowserContext."""
        browser = ExtensionBrowser()
        ctx = await browser.new_context()

        self.assertIsInstance(ctx, ExtensionBrowserContext)
        self.assertEqual(ctx.server_url, "http://127.0.0.1:8765")
        await ctx.close()
        await browser.close()

    async def test_extension_browser_context_manager(self):
        """Verify async context manager interface."""
        async with ExtensionBrowser() as browser:
            self.assertIsInstance(browser, ExtensionBrowser)


class TestExtensionAgentIntegrationUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for BrowserUseAgent with injected ExtensionBrowser and ExtensionBrowserContext."""

    def setUp(self):
        self.sample_data = get_sample_fixture()
        self.mock_llm = create_mock_llm()

    async def test_agent_auto_injects_extension_browser_and_context(self):
        """Verify BrowserUseAgent automatically injects ExtensionBrowser and ExtensionBrowserContext."""
        agent = BrowserUseAgent(
            task="Verify automatic injection",
            llm=self.mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        self.assertIsInstance(agent.browser, ExtensionBrowser)
        self.assertIsInstance(agent.browser_context, ExtensionBrowserContext)
        self.assertTrue(agent.injected_browser, "injected_browser flag must be True")
        self.assertTrue(agent.injected_browser_context, "injected_browser_context flag must be True")

        # Verify zero Playwright browser instances
        self.assertFalse(isinstance(agent.browser, Browser))
        self.assertIsNone(agent.browser_context.session)

        await agent.close()

    async def test_agent_preserves_explicit_injections(self):
        """Verify passing explicit browser and context instances overrides defaults."""
        custom_browser = ExtensionBrowser()
        custom_context = ExtensionBrowserContext(server_url="http://127.0.0.1:9999")

        agent = BrowserUseAgent(
            task="Verify explicit injection",
            llm=self.mock_llm,
            browser=custom_browser,
            browser_context=custom_context,
            enable_memory=False,
            use_vision=False,
        )

        self.assertIs(agent.browser, custom_browser)
        self.assertIs(agent.browser_context, custom_context)
        self.assertEqual(agent.browser_context.server_url, "http://127.0.0.1:9999")

        await agent.close()

    async def test_agent_can_retrieve_browser_state(self):
        """Verify agent can query and reconstruct BrowserState through the extension context."""
        agent = BrowserUseAgent(
            task="Verify state retrieval",
            llm=self.mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        with patch.object(agent.browser_context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }

            state = await agent.browser_context.get_state()
            self.assertIsInstance(state, BrowserState)
            self.assertIsInstance(state.element_tree, DOMElementNode)
            self.assertGreater(len(state.selector_map), 0)

            page = await agent.browser_context.get_current_page()
            self.assertIsInstance(page, ExtensionTabProxy)
            self.assertEqual(page.url, self.sample_data["url"])

        await agent.close()

    async def test_agent_completes_one_reasoning_step(self):
        """Verify agent executes a full reasoning step, updates history, and finishes cleanly."""
        agent = BrowserUseAgent(
            task="Test single reasoning step",
            llm=self.mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        with patch.object(agent.browser_context, "execute_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "type": "ACTION_RESULT",
                "action": "GET_DOM_STATE",
                "success": True,
                "result": self.sample_data,
            }

            async def mock_get_next_action(input_messages):
                return agent.AgentOutput(
                    current_state={
                        "evaluation_previous_goal": "Initialize step",
                        "memory": "Test memory",
                        "next_goal": "Complete verification",
                    },
                    action=[{"done": {"text": "Reasoning step passed", "success": True}}],
                )

            with patch.object(agent, "get_next_action", side_effect=mock_get_next_action):
                step_info = AgentStepInfo(step_number=0, max_steps=1)
                await agent.step(step_info)

                self.assertEqual(len(agent.state.history.history), 1)
                self.assertTrue(agent.state.history.is_done())
                self.assertEqual(
                    agent.state.last_result[0].extracted_content,
                    "Reasoning step passed",
                )

        await agent.close()

    async def test_agent_clean_shutdown(self):
        """Verify agent shutdown closes underlying extension context resources idempotently."""
        agent = BrowserUseAgent(
            task="Test clean shutdown",
            llm=self.mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        # Trigger client creation
        client = agent.browser_context._get_client()
        self.assertFalse(client.is_closed)

        await agent.close()
        self.assertIsNone(agent.browser_context._http_client)

        # Double close should be idempotent
        await agent.close()


class TestExtensionAgentLive(unittest.IsolatedAsyncioTestCase):
    """Integration test executing BrowserUseAgent with the live Chrome Extension."""

    async def _is_live_environment_ready(self) -> bool:
        """Checks if FastAPI server and Chrome Extension are connected."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get("http://127.0.0.1:8765/health")
                if res.status_code == 200:
                    data = res.json()
                    return data.get("extension_connected", False)
        except Exception:
            return False
        return False

    async def test_live_agent_state_retrieval_and_reasoning_step(self):
        """Executes one real agent reasoning step against the live Chrome browser."""
        ready = await self._is_live_environment_ready()
        if not ready:
            self.skipTest(
                "Live Chrome Extension is not connected on ws://127.0.0.1:8765. "
                "Skipping live agent test."
            )

        mock_llm = create_mock_llm()
        agent = BrowserUseAgent(
            task="Live Chrome reasoning step test",
            llm=mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        try:
            # 1. Verify live state retrieval through agent's context
            state = await agent.browser_context.get_state()
            self.assertIsInstance(state, BrowserState)
            self.assertGreater(len(state.selector_map), 0)

            # 2. Complete one reasoning step using mock action completion
            async def mock_get_next_action(input_messages):
                return agent.AgentOutput(
                    current_state={
                        "evaluation_previous_goal": "Live check",
                        "memory": "Live memory",
                        "next_goal": "Complete live step",
                    },
                    action=[{"done": {"text": "Live reasoning step verified", "success": True}}],
                )

            with patch.object(agent, "get_next_action", side_effect=mock_get_next_action):
                step_info = AgentStepInfo(step_number=0, max_steps=1)
                await agent.step(step_info)

                self.assertEqual(len(agent.state.history.history), 1)
                self.assertTrue(agent.state.history.is_done())
                self.assertEqual(
                    agent.state.last_result[0].extracted_content,
                    "Live reasoning step verified",
                )
        finally:
            await agent.close()


if __name__ == "__main__":
    unittest.main()
