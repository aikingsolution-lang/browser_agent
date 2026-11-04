"""
Regression test suite for WebUI lifecycle, concurrency, and context reuse (BUG 2).

Tests:
1. Clear during an active task (ensures no AttributeError when bu_agent is set to None).
2. Stop during an active task (safe shutdown and state preservation).
3. Clear followed by a new Submit (fresh context and agent initialization).
4. Repeated Clear calls (safe idempotence).
5. Context reuse with keep_browser_open enabled (preserves session, validates live tab).
6. Agent-owned tab cleanup (only agent tabs closed on Clear / finish).
7. Concurrent submissions protection (prevents corrupting shared WebUI manager state).
"""

import asyncio
import os
import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import gradio as gr
from langchain_core.language_models.chat_models import BaseChatModel

from src.agent.browser_use.browser_use_agent import BrowserUseAgent
from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController
from src.webui.components.browser_use_agent_tab import (
    handle_clear,
    handle_stop,
    handle_submit,
    run_agent_task,
)
from src.webui.webui_manager import WebuiManager


def create_test_components(manager: WebuiManager) -> Dict[Any, Any]:
    """Helper to populate mock Gradio components in WebuiManager."""
    user_input = gr.Textbox()
    run_btn = gr.Button()
    stop_btn = gr.Button()
    pause_btn = gr.Button()
    clear_btn = gr.Button()
    chatbot = gr.Chatbot()
    hist_file = gr.File()
    gif_file = gr.Image()
    browser_view = gr.HTML()

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
    }

    return {
        user_input: "Test task instruction",
    }


class TestWebUILifecycleRegression(unittest.IsolatedAsyncioTestCase):
    """Lifecycle and concurrency regression tests for WebUI."""

    def setUp(self):
        self.manager = WebuiManager()
        self.manager.init_browser_use_agent()
        self.components = create_test_components(self.manager)

        # Mock health endpoint so pre-flight check succeeds
        self.mock_health_patcher = patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(
                return_value=MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"status": "ok", "extension_connected": True}),
                )
            ),
        )
        self.mock_health_patcher.start()

        # Mock LLM
        self.mock_llm = MagicMock(spec=BaseChatModel)
        self.mock_llm.model_name = "test-model"
        self.mock_llm._llm_type = "mock"
        self.mock_llm_patcher = patch(
            "src.webui.components.browser_use_agent_tab._initialize_llm",
            new=AsyncMock(return_value=self.mock_llm),
        )
        self.mock_llm_patcher.start()

    def tearDown(self):
        self.mock_health_patcher.stop()
        self.mock_llm_patcher.stop()

    async def test_01_clear_during_active_task_race_condition(self):
        """
        Verify that calling handle_clear() while run_agent_task() is executing
        does NOT cause AttributeError: 'NoneType' object has no attribute 'state'
        around line 698 of browser_use_agent_tab.py.
        """
        task_started = asyncio.Event()
        clear_called = asyncio.Event()

        # Simulate a long-running agent step
        original_run = BrowserUseAgent.run

        async def slow_run(agent_self, *args, **kwargs):
            task_started.set()
            # Wait until clear has been executed
            await clear_called.wait()
            # Yield control so run_agent_task wakes up and hits the finalization code
            await asyncio.sleep(0.05)
            return MagicMock(history=[])

        caught_exceptions = []

        with patch.object(BrowserUseAgent, "run", slow_run):
            async def run_task_wrapper():
                try:
                    async for _ in run_agent_task(self.manager, self.components):
                        pass
                except Exception as ex:
                    caught_exceptions.append(ex)

            # Start task in background
            gen_task = asyncio.create_task(run_task_wrapper())

            # Wait for task to initialize and start running
            await task_started.wait()

            # Ensure bu_agent is initially populated
            self.assertIsNotNone(self.manager.bu_agent)

            # User clicks Clear button while task is running!
            await handle_clear(self.manager)
            clear_called.set()

            # Wait for task generator to exit
            await gen_task

        # Assert no AttributeError occurred in the task loop
        for exc in caught_exceptions:
            self.assertNotIsInstance(
                exc,
                AttributeError,
                f"AttributeError encountered during clear race: {exc}",
            )
        self.assertEqual(len(caught_exceptions), 0, f"Task encountered errors: {caught_exceptions}")
        # Assert no AttributeError was captured in chat history
        self.assertFalse(
            any("AttributeError" in str(msg.get("content", "")) for msg in self.manager.bu_chat_history),
            f"AttributeError found in chat history: {self.manager.bu_chat_history}",
        )

    async def test_02_stop_during_active_task(self):
        """Verify handle_stop() cleanly signals agent without crashing or corrupting state."""
        agent_started = asyncio.Event()

        async def pause_then_stop(agent_self, *args, **kwargs):
            agent_started.set()
            while not agent_self.state.stopped:
                await asyncio.sleep(0.05)
            return MagicMock(history=[])

        with patch.object(BrowserUseAgent, "run", pause_then_stop):
            async def drain():
                async for _ in handle_submit(self.manager, self.components):
                    pass

            task_future = asyncio.create_task(drain())
            await agent_started.wait()

            # Call stop
            stop_res = await handle_stop(self.manager)
            self.assertIn("Stopping...", str(stop_res))

            await task_future

    async def test_03_repeated_clear_calls_are_safe(self):
        """Verify calling handle_clear() multiple times in succession is safe (idempotent)."""
        res1 = await handle_clear(self.manager)
        self.assertIsInstance(res1, dict)

        res2 = await handle_clear(self.manager)
        self.assertIsInstance(res2, dict)

        res3 = await handle_clear(self.manager)
        self.assertIsInstance(res3, dict)

        self.assertIsNone(self.manager.bu_agent)
        self.assertIsNone(self.manager.bu_current_task)

    async def test_04_clear_followed_by_new_submit(self):
        """Verify that after Clear, a subsequent Submit initializes a fresh agent and context."""
        with patch.object(BrowserUseAgent, "run", AsyncMock(return_value=MagicMock(history=[]))):
            # Run first task
            async for _ in run_agent_task(self.manager, self.components):
                pass

            first_context = self.manager.bu_browser_context
            self.assertIsNotNone(first_context)

            # Clear
            await handle_clear(self.manager)
            self.assertIsNone(self.manager.bu_agent)
            self.assertIsNone(self.manager.bu_browser_context)

            # Run second task
            async for _ in run_agent_task(self.manager, self.components):
                pass

            second_context = self.manager.bu_browser_context
            self.assertIsNotNone(second_context)
            self.assertIsNot(first_context, second_context)

    async def test_05_context_reuse_with_keep_browser_open(self):
        """
        Verify that when keep_browser_open is True:
        - The context is preserved across tasks.
        - If the context was closed, Submit creates a fresh context instead of reusing closed one.
        """
        # Set keep_browser_open component to True
        keep_open_comp = gr.Checkbox(value=True)
        self.manager.id_to_component["browser_settings.keep_browser_open"] = keep_open_comp
        self.components[keep_open_comp] = True

        with patch.object(BrowserUseAgent, "run", AsyncMock(return_value=MagicMock(history=[]))):
            # Task 1
            async for _ in run_agent_task(self.manager, self.components):
                pass

            context_1 = self.manager.bu_browser_context
            self.assertIsNotNone(context_1)
            self.assertFalse(context_1.is_closed)

            # Task 2 (keep_browser_open=True): reuses active context
            async for _ in run_agent_task(self.manager, self.components):
                pass

            context_2 = self.manager.bu_browser_context
            self.assertIs(context_1, context_2, "Active context should be reused when keep_browser_open=True")

            # Now close context_2 (e.g. externally or via settings change)
            await context_2.close()
            self.assertTrue(context_2.is_closed)

            # Task 3: must NOT reuse closed context!
            async for _ in run_agent_task(self.manager, self.components):
                pass

            context_3 = self.manager.bu_browser_context
            self.assertIsNot(context_2, context_3, "Closed context must not be reused")
            self.assertFalse(context_3.is_closed)

    async def test_06_agent_owned_tab_cleanup_on_clear(self):
        """Verify handle_clear() cleans up all agent-owned tabs and leaves human tabs untouched."""
        context = ExtensionBrowserContext()
        closed_tabs = []

        async def mock_execute(action, params=None):
            if action == "CLOSE_TAB":
                closed_tabs.append(params["tabId"])
                return {"success": True, "result": {"tabId": params["tabId"], "closed": True}}
            return {"success": True}

        context.execute_action = AsyncMock(side_effect=mock_execute)
        context.agent_tab_ids = {201, 202}
        context._current_tab_id = 201

        self.manager.bu_browser_context = context

        await handle_clear(self.manager)

        self.assertIn(201, closed_tabs)
        self.assertIn(202, closed_tabs)
        self.assertNotIn(999, closed_tabs)  # Human tab 999 never closed
        self.assertEqual(len(context.agent_tab_ids), 0)

    async def test_07_concurrent_submissions_prevented(self):
        """Verify that rapid concurrent calls to handle_submit do not corrupt shared state."""
        task_running = asyncio.Event()

        async def slow_generator(*args, **kwargs):
            task_running.set()
            await asyncio.sleep(0.3)
            yield {"update": 1}

        with patch("src.webui.components.browser_use_agent_tab.run_agent_task", side_effect=slow_generator):
            # First submission starts
            sub1 = handle_submit(self.manager, self.components)
            # Advance to set running flag
            first_update = await sub1.__anext__()

            # Second submission attempted while first is running
            sub2 = handle_submit(self.manager, self.components)
            updates2 = []
            async for u in sub2:
                updates2.append(u)

            # sub2 should be blocked (yields empty update without starting new task)
            self.assertEqual(updates2, [{}])

            # Clean up generator sub1
            try:
                await sub1.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
