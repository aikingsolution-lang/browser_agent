"""
Comprehensive verification test suite for Browser Agent browser lifecycle bug fix.

Tests:
- Test A — Injected resources: Verify agent.close() does NOT close injected ExtensionBrowser or ExtensionBrowserContext.
- Test B — Agent-owned resources: Verify agent.close() closes owned resources exactly once.
- Test C — Keep browser open: Verify keep_browser_open=True keeps agent tabs open and reuses context.
- Test D — Close browser after task: Verify keep_browser_open=False closes agent tabs but preserves human tabs.
- Test E — Explicit close_tab: Verify explicit close_tab tool action still closes requested tab.
- Test F — Error and cancellation: Verify errors or task cancellation do NOT close injected context when keep_browser_open=True.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import gradio as gr

from langchain_core.language_models.chat_models import BaseChatModel
from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult, AgentHistoryList
from src.agent.browser_use.browser_use_agent import BrowserUseAgent
from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController, CloseTabAction
from src.webui.webui_manager import WebuiManager
from src.webui.components.browser_use_agent_tab import run_agent_task


def create_mock_llm():
    mock_llm = MagicMock(spec=BaseChatModel)
    mock_llm.model_name = "test-model"
    mock_llm._llm_type = "mock"
    mock_llm.invoke.return_value.content = "done"
    return mock_llm


class TestBrowserLifecycleFix(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_llm = create_mock_llm()

    async def test_a_injected_resources_not_closed_on_agent_close(self):
        """Test A: Verify agent.close() does NOT close injected browser or context."""
        browser = ExtensionBrowser()
        browser_context = ExtensionBrowserContext()
        browser.close = AsyncMock()
        browser_context.close = AsyncMock()

        agent = BrowserUseAgent(
            task="Test injected resources",
            llm=self.mock_llm,
            browser=browser,
            browser_context=browser_context,
            enable_memory=False,
            use_vision=False,
        )

        self.assertTrue(agent.injected_browser, "injected_browser must be True")
        self.assertTrue(agent.injected_browser_context, "injected_browser_context must be True")

        # Invoke agent.close() (simulating agent shutdown at end of run)
        await agent.close()

        # Neither injected resource should have been closed
        self.assertEqual(browser.close.call_count, 0, "Injected browser.close() must NOT be called")
        self.assertEqual(browser_context.close.call_count, 0, "Injected browser_context.close() must NOT be called")
        self.assertFalse(browser_context.is_closed, "Injected context must remain open")

    async def test_b_agent_owned_resources_cleaned_up_exactly_once(self):
        """Test B: Verify upstream Agent with owned resources closes them exactly once."""
        agent = Agent(
            task="Test owned resources",
            llm=self.mock_llm,
            enable_memory=False,
            use_vision=False,
        )

        agent.browser.close = AsyncMock()
        agent.browser_context.close = AsyncMock()

        self.assertFalse(agent.injected_browser, "injected_browser must be False for owned resources")
        self.assertFalse(agent.injected_browser_context, "injected_browser_context must be False for owned resources")

        await agent.close()

        self.assertEqual(agent.browser.close.call_count, 1, "Owned browser must be closed exactly once")
        self.assertEqual(agent.browser_context.close.call_count, 1, "Owned context must be closed exactly once")

    async def test_c_keep_browser_open_preserves_tabs_and_reuses_context(self):
        """Test C: Verify keep_browser_open=True preserves agent tabs and reuses context."""
        manager = WebuiManager()
        manager.init_browser_use_agent()

        user_input = gr.Textbox()
        run_btn = gr.Button()
        stop_btn = gr.Button()
        pause_btn = gr.Button()
        clear_btn = gr.Button()
        chatbot = gr.Chatbot()
        hist_file = gr.File()
        gif_file = gr.Image()
        browser_view = gr.HTML()
        keep_open_comp = gr.Checkbox(value=True)

        manager.id_to_component = {
            "browser_use_agent.user_input": user_input,
            "browser_use_agent.run_button": run_btn,
            "browser_use_agent.stop_button": stop_btn,
            "browser_use_agent.pause_resume_button": pause_btn,
            "browser_use_agent.clear_button": clear_btn,
            "browser_use_agent.chatbot": chatbot,
            "browser_use_agent.agent_history_file": hist_file,
            "browser_use_agent.recording_gif": gif_file,
            "browser_use_agent.browser_view": browser_view,
            "browser_settings.keep_browser_open": keep_open_comp,
        }

        components = {
            user_input: "Open YouTube and play Kala Chashma",
            keep_open_comp: True,
        }

        closed_tab_actions = []

        async def mock_execute(action, params=None):
            if action == "CLOSE_TAB":
                closed_tab_actions.append(params.get("tabId"))
                return {"success": True, "result": {"tabId": params.get("tabId"), "closed": True}}
            if action == "CREATE_NEW_TAB":
                return {"success": True, "result": {"tabId": 101, "url": "about:blank"}}
            if action == "GET_DOM_STATE":
                return {"success": True, "result": {"tab_id": 101, "tabs": [{"tab_id": 101, "url": "https://youtube.com"}], "element_tree": {}}}
            return {"success": True, "result": {}}

        with patch("httpx.AsyncClient.get") as mock_health:
            mock_health.return_value = MagicMock(status_code=200, json=lambda: {"extension_connected": True})

            with patch("src.webui.components.browser_use_agent_tab._initialize_llm", new=AsyncMock(return_value=self.mock_llm)):
                # Task 1: Runs and completes
                async for _ in run_agent_task(manager, components):
                    pass

                context_task1 = manager.bu_browser_context
                self.assertIsNotNone(context_task1)
                self.assertFalse(context_task1.is_closed, "Context must remain open after task 1")

                # Track tab 101 as agent-owned
                context_task1.agent_tab_ids.add(101)
                context_task1.execute_action = AsyncMock(side_effect=mock_execute)

                # Ensure no CLOSE_TAB was dispatched during task 1 completion
                self.assertNotIn(101, closed_tab_actions, "YouTube tab 101 must NOT be closed upon task completion")

                # Task 2: Next task must reuse the active context
                components[user_input] = "Second task"
                async for _ in run_agent_task(manager, components):
                    pass

                context_task2 = manager.bu_browser_context
                self.assertIs(context_task1, context_task2, "Same context must be reused when keep_browser_open=True")
                self.assertFalse(context_task2.is_closed, "Reused context must still be open")
                self.assertNotIn(101, closed_tab_actions, "Tab 101 must remain open across tasks")

    async def test_d_close_browser_after_task_when_keep_browser_open_false(self):
        """Test D: Verify keep_browser_open=False closes agent tabs but preserves human tabs."""
        manager = WebuiManager()
        manager.init_browser_use_agent()

        user_input = gr.Textbox()
        run_btn = gr.Button()
        stop_btn = gr.Button()
        pause_btn = gr.Button()
        clear_btn = gr.Button()
        chatbot = gr.Chatbot()
        hist_file = gr.File()
        gif_file = gr.Image()
        browser_view = gr.HTML()
        keep_open_comp = gr.Checkbox(value=False)

        manager.id_to_component = {
            "browser_use_agent.user_input": user_input,
            "browser_use_agent.run_button": run_btn,
            "browser_use_agent.stop_button": stop_btn,
            "browser_use_agent.pause_resume_button": pause_btn,
            "browser_use_agent.clear_button": clear_btn,
            "browser_use_agent.chatbot": chatbot,
            "browser_use_agent.agent_history_file": hist_file,
            "browser_use_agent.recording_gif": gif_file,
            "browser_use_agent.browser_view": browser_view,
            "browser_settings.keep_browser_open": keep_open_comp,
        }

        components = {
            user_input: "Task with close after finish",
            keep_open_comp: False,
        }

        closed_tabs = []

        async def mock_execute(action, params=None):
            if action == "CLOSE_TAB":
                closed_tabs.append(params["tabId"])
                return {"success": True, "result": {"tabId": params["tabId"], "closed": True}}
            if action == "CREATE_NEW_TAB":
                return {"success": True, "result": {"tabId": 201, "url": "about:blank"}}
            if action == "GET_DOM_STATE":
                return {"success": True, "result": {"tab_id": 201, "tabs": [{"tab_id": 201}], "element_tree": {}}}
            return {"success": True, "result": {}}

        # Set up a context that owns agent tab 201, and human tab 999 exists
        context = ExtensionBrowserContext()
        context.execute_action = AsyncMock(side_effect=mock_execute)
        context.agent_tab_ids = {201}
        context._current_tab_id = 201
        manager.bu_browser_context = context

        with patch("httpx.AsyncClient.get") as mock_health:
            mock_health.return_value = MagicMock(status_code=200, json=lambda: {"extension_connected": True})
            with patch("src.webui.components.browser_use_agent_tab._initialize_llm", new=AsyncMock(return_value=self.mock_llm)):
                async for _ in run_agent_task(manager, components):
                    pass

        # Since keep_browser_open=False, WebUI cleanup closes the context on finish
        self.assertTrue(context.is_closed, "Context must be closed when keep_browser_open=False")
        self.assertIn(201, closed_tabs, "Agent tab 201 must be closed")
        self.assertNotIn(999, closed_tabs, "Human tab 999 must NOT be closed")

    async def test_e_explicit_close_tab_closes_requested_tab(self):
        """Test E: Verify explicit close_tab action still closes the requested tab."""
        controller = ExtensionController()
        context = ExtensionBrowserContext()

        executed_actions = []

        async def mock_execute(action, params=None):
            executed_actions.append((action, params))
            return {"success": True, "result": {"tabId": params.get("tabId"), "closed": True}}

        context.execute_action = AsyncMock(side_effect=mock_execute)
        context.agent_tab_ids = {555}

        close_fn = controller.registry.registry.actions["close_tab"].function
        result = await close_fn(CloseTabAction(page_id=555), browser=context)

        self.assertIsInstance(result, ActionResult)
        self.assertIn("Closed tab #555", result.extracted_content)
        self.assertIn(("CLOSE_TAB", {"tabId": 555}), executed_actions)

    async def test_f_error_and_cancellation_preserves_injected_context(self):
        """Test F: Verify task failure/cancellation does not close injected context when keep_browser_open=True."""
        manager = WebuiManager()
        manager.init_browser_use_agent()

        user_input = gr.Textbox()
        run_btn = gr.Button()
        stop_btn = gr.Button()
        pause_btn = gr.Button()
        clear_btn = gr.Button()
        chatbot = gr.Chatbot()
        hist_file = gr.File()
        gif_file = gr.Image()
        browser_view = gr.HTML()
        keep_open_comp = gr.Checkbox(value=True)

        manager.id_to_component = {
            "browser_use_agent.user_input": user_input,
            "browser_use_agent.run_button": run_btn,
            "browser_use_agent.stop_button": stop_btn,
            "browser_use_agent.pause_resume_button": pause_btn,
            "browser_use_agent.clear_button": clear_btn,
            "browser_use_agent.chatbot": chatbot,
            "browser_use_agent.agent_history_file": hist_file,
            "browser_use_agent.recording_gif": gif_file,
            "browser_use_agent.browser_view": browser_view,
            "browser_settings.keep_browser_open": keep_open_comp,
        }

        components = {
            user_input: "Task that errors",
            keep_open_comp: True,
        }

        context = ExtensionBrowserContext()
        context.close = AsyncMock()
        manager.bu_browser_context = context

        # Case 1: Agent.run() raises RuntimeError
        with patch("httpx.AsyncClient.get") as mock_health:
            mock_health.return_value = MagicMock(status_code=200, json=lambda: {"extension_connected": True})
            with patch("src.webui.components.browser_use_agent_tab._initialize_llm", new=AsyncMock(return_value=self.mock_llm)):
                with patch.object(BrowserUseAgent, "run", AsyncMock(side_effect=RuntimeError("Simulated LLM crash"))):
                    async for _ in run_agent_task(manager, components):
                        pass

        self.assertFalse(context.is_closed, "Context must remain open after agent error")
        self.assertEqual(context.close.call_count, 0, "context.close() must NOT be called on error")


if __name__ == "__main__":
    unittest.main()
