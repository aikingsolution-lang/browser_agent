"""
ExtensionController for Browser Use 0.1.48 with Chrome Extension backend.

Replaces Playwright browser actions with commands dispatched over HTTP to
FastAPI /api/v1/action and executed in Chrome via WebSocket.
Subclasses CustomController to retain custom tools (ask_for_assistant, upload_file, MCP).
"""

import asyncio
import inspect
import logging
from typing import Any, Callable, Type

from pydantic import BaseModel
from browser_use.agent.views import ActionResult
from browser_use.controller.views import (
    ClickElementAction,
    CloseTabAction,
    DoneAction,
    GoToUrlAction,
    InputTextAction,
    NoParamsAction,
    OpenTabAction,
    ScrollAction,
    SearchGoogleAction,
    SendKeysAction,
    SwitchTabAction,
)
from langchain_core.language_models.chat_models import BaseChatModel

from src.browser.extension_context import ExtensionBrowserContext
from src.controller.custom_controller import CustomController

logger = logging.getLogger(__name__)

# Actions in upstream Browser Use Controller that depend on Playwright
PLAYWRIGHT_ACTIONS = [
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
    "extract_content",
    "upload_file",
    "scroll_to_text",
    "save_pdf",
    "get_dropdown_options",
    "select_dropdown_option",
    "drag_drop",
    "get_sheet_contents",
    "select_cell_or_range",
    "get_range_contents",
    "clear_selected_range",
    "input_selected_cell_text",
    "update_range_contents",
]


async def resolve_chrome_tab_id(browser: ExtensionBrowserContext, page_id: int) -> int:
    """
    Resolves Browser Use page_id into an actual Chrome tab ID:
    1. If page_id is already an exact integer in browser.agent_tab_ids, fast-path return.
    2. page_id == -1 -> most recently opened / last agent-owned tab.
    3. Exact match against known agent-owned Chrome tab IDs.
    4. Positional 0-based index fallback only among agent-owned tabs.
    """
    # Fast path: If page_id is an exact known agent-owned tab
    if browser.agent_tab_ids and page_id in browser.agent_tab_ids:
        return page_id

    res = await browser.execute_action("GET_TABS")
    tabs = res.get("result", [])
    if isinstance(tabs, dict):
        tabs = [tabs]
    if not tabs:
        raise RuntimeError("No open Chrome tabs found")

    tab_ids = [t["tabId"] for t in tabs if isinstance(t, dict) and "tabId" in t]

    # Restrict to agent-owned tabs if tracked (DEF-04)
    if browser.agent_tab_ids:
        owned_ids = [tid for tid in tab_ids if tid in browser.agent_tab_ids]
        if owned_ids:
            tab_ids = owned_ids

    if not tab_ids:
        raise RuntimeError("No agent-owned Chrome tabs available")

    # Rule 1: -1 refers to the newest / last tab
    if page_id == -1:
        return tab_ids[-1]

    # Rule 2: Exact Chrome tab ID match
    if page_id in tab_ids:
        return page_id

    # Rule 3: Positional 0-based index fallback if unambiguous
    if 0 <= page_id < len(tab_ids):
        return tab_ids[page_id]

    raise ValueError(f"Could not resolve tab for page_id={page_id} among agent tabs {tab_ids}")


class ExtensionController(CustomController):
    """
    Controller executing browser actions via FastAPI WebSocket bridge to Chrome Extension.
    Inherits ask_for_assistant and MCP functionality from CustomController.
    """

    def __init__(
        self,
        exclude_actions: list[str] = [],
        output_model: Type[BaseModel] | None = None,
        ask_assistant_callback: Any = None,
    ):
        # Merge caller's exclude_actions with PLAYWRIGHT_ACTIONS so Playwright actions are never registered
        merged_excludes = list(set(list(exclude_actions) + PLAYWRIGHT_ACTIONS))
        super().__init__(
            exclude_actions=merged_excludes,
            output_model=output_model,
            ask_assistant_callback=ask_assistant_callback,
        )

        # Restore caller's exclude_actions so extension actions can register on self.registry
        self.registry.exclude_actions = list(exclude_actions)

        self._register_extension_actions()

    def _register_extension_actions(self) -> None:
        """Registers extension-backed implementations of all primary browser actions."""

        # ------------------------------------------------------------------
        # 1. Navigation Actions
        # ------------------------------------------------------------------
        @self.registry.action(
            "Search the query in Google in the current tab, the query should be a search query like humans search in Google, concrete and not vague or super long. More the single most important items. ",
            param_model=SearchGoogleAction,
        )
        async def search_google(params: SearchGoogleAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            search_url = f"https://www.google.com/search?q={params.query}&udm=14"
            await browser.execute_action("NAVIGATE", {"url": search_url, "tabId": browser._current_tab_id})
            browser.invalidate_state()
            msg = f'🔍  Searched for "{params.query}" in Google'
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action("Navigate to URL in the current tab", param_model=GoToUrlAction)
        async def go_to_url(params: GoToUrlAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            await browser.execute_action("NAVIGATE", {"url": params.url, "tabId": browser._current_tab_id})
            browser.invalidate_state()
            msg = f"🔗  Navigated to {params.url}"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action("Go back", param_model=NoParamsAction)
        async def go_back(_: NoParamsAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            await browser.execute_action("GO_BACK", {"tabId": browser._current_tab_id})
            browser.invalidate_state()
            msg = "🔙  Navigated back"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        # ------------------------------------------------------------------
        # 2. Element Interaction Actions
        # ------------------------------------------------------------------
        @self.registry.action("Click element by index", param_model=ClickElementAction)
        async def click_element_by_index(params: ClickElementAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            selector_map = await browser.get_selector_map()

            if params.index not in selector_map:
                msg = f"Element with index {params.index} does not exist in selector map. Page structure may have changed."
                logger.warning(msg)
                return ActionResult(error=msg, include_in_memory=True)

            element_node = selector_map[params.index]

            if not element_node.xpath:
                msg = f"Element with index {params.index} has no valid XPath."
                logger.error(msg)
                return ActionResult(error=msg, include_in_memory=True)

            logger.info(f"🖱️  Clicking element with index {params.index} (tag: {element_node.tag_name}, xpath: {element_node.xpath})")
            
            await browser.execute_action("CLICK", {
                "xpath": element_node.xpath,
                "tabId": browser._current_tab_id,
            })

            # Invalidate state: clicking may cause navigation or DOM re-rendering
            browser.invalidate_state()

            element_desc = element_node.get_all_text_till_next_clickable_element(max_depth=2) or element_node.tag_name
            msg = f"🖱️  Clicked element with index {params.index}: {element_desc}"
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action(
            "Input text into a input interactive element",
            param_model=InputTextAction,
        )
        async def input_text(params: InputTextAction, browser: ExtensionBrowserContext, has_sensitive_data: bool = False):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            selector_map = await browser.get_selector_map()

            if params.index not in selector_map:
                msg = f"Element with index {params.index} does not exist in selector map."
                logger.warning(msg)
                return ActionResult(error=msg, include_in_memory=True)

            element_node = selector_map[params.index]

            if not element_node.xpath:
                msg = f"Element with index {params.index} has no valid XPath."
                logger.error(msg)
                return ActionResult(error=msg, include_in_memory=True)

            logger.info(f"⌨️  Typing into element index {params.index} (xpath: {element_node.xpath})")

            await browser.execute_action("TYPE", {
                "xpath": element_node.xpath,
                "text": params.text,
                "tabId": browser._current_tab_id,
            })

            # Invalidate state: typing may trigger autocompletion or DOM modifications
            browser.invalidate_state()

            display_text = "***" if has_sensitive_data else params.text
            msg = f'⌨️  Inputted "{display_text}" into element with index {params.index}'
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action(
            "Upload file to interactive element with file path",
        )
        async def upload_file(index: int, path: str, browser: ExtensionBrowserContext, available_file_paths: list[str] = []):
            """
            In Chrome Extension architecture without Playwright, arbitrary local file paths cannot
            be programmatically attached to file inputs due to browser security sandbox constraints.
            Delegates to human assistant via ask_for_assistant workflow.
            """
            msg = (
                f"Autonomous local file upload for '{path}' at element index {index} is restricted "
                f"by Chrome Extension security policies. Please use 'ask_for_assistant' so the user "
                f"can select the file in the browser file dialog."
            )
            logger.warning(msg)
            return ActionResult(error=msg, include_in_memory=True)

        # ------------------------------------------------------------------
        # 3. Content Extraction (Extension-backed Replacement for DEF-01)
        # ------------------------------------------------------------------
        @self.registry.action(
            "Extract page content to retrieve specific information from the page, e.g. all company names, a specific description, all information about, links with companies in structured format or simply links",
        )
        async def extract_content(
            goal: str,
            should_strip_link_urls: bool,
            browser: ExtensionBrowserContext,
            page_extraction_llm: BaseChatModel | None = None,
        ):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            res = await browser.execute_action("GET_PAGE_CONTENT", {
                "tabId": browser._current_tab_id,
                "stripLinkUrls": should_strip_link_urls,
            })
            raw_text = res.get("result", {}).get("content", "")

            # If an extraction LLM is available, summarize according to goal
            if page_extraction_llm and goal and goal.strip():
                try:
                    from langchain_core.prompts import PromptTemplate
                    prompt = (
                        "Your task is to extract the content of the page. You will be given a page and a goal and you should extract "
                        "all relevant information around this goal from the page. If the goal is vague, summarize the page. "
                        "Respond in json format. Extraction goal: {goal}, Page: {page}"
                    )
                    template = PromptTemplate(input_variables=["goal", "page"], template=prompt)
                    output = await page_extraction_llm.ainvoke(template.format(goal=goal, page=raw_text[:25000]))
                    msg = f"📄 Extracted from page:\n{output.content}\n"
                    logger.info(msg)
                    return ActionResult(extracted_content=msg, include_in_memory=True)
                except Exception as e:
                    logger.warning(f"Error extracting content via LLM: {e}")

            msg = f"📄 Extracted from page:\n{raw_text}\n"
            return ActionResult(extracted_content=msg, include_in_memory=True)

        # ------------------------------------------------------------------
        # 4. Tab Management Actions (with Strict Ownership DEF-04)
        # ------------------------------------------------------------------
        @self.registry.action("Switch tab", param_model=SwitchTabAction)
        async def switch_tab(params: SwitchTabAction, browser: ExtensionBrowserContext):
            resolved_tab_id = await resolve_chrome_tab_id(browser, params.page_id)
            await browser.execute_action("SWITCH_TAB", {"tabId": resolved_tab_id})

            browser.adopt_tab(resolved_tab_id)
            browser.invalidate_state()

            msg = f"🔄  Switched to tab {resolved_tab_id}"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action("Open url in new tab", param_model=OpenTabAction)
        async def open_tab(params: OpenTabAction, browser: ExtensionBrowserContext):
            try:
                res = await browser.execute_action("CREATE_NEW_TAB", {"url": params.url})
            except RuntimeError as err:
                if "Unsupported action: CREATE_NEW_TAB" in str(err):
                    res = await browser.execute_action("NAVIGATE", {"url": params.url, "newTab": True})
                else:
                    raise

            result_data = res.get("result", {})
            new_tab_id = result_data.get("tabId")

            if new_tab_id:
                browser.adopt_tab(new_tab_id)

            browser.invalidate_state()

            msg = f"🌐  Opened {params.url} in new tab (tab ID: {new_tab_id})"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action("Close an existing tab", param_model=CloseTabAction)
        async def close_tab(params: CloseTabAction, browser: ExtensionBrowserContext):
            resolved_tab_id = await resolve_chrome_tab_id(browser, params.page_id)
            await browser.execute_action("CLOSE_TAB", {"tabId": resolved_tab_id})

            # Update agent owned tabs (DEF-04)
            browser.agent_tab_ids.discard(resolved_tab_id)

            # If closed tab was current tab, select another agent-owned tab or set None
            if browser._current_tab_id == resolved_tab_id:
                if browser.agent_tab_ids:
                    browser._current_tab_id = list(browser.agent_tab_ids)[-1]
                else:
                    browser._current_tab_id = None

            browser.invalidate_state()

            msg = f"❌  Closed tab #{resolved_tab_id}"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        # ------------------------------------------------------------------
        # 5. Page Movement & Keys Actions
        # ------------------------------------------------------------------
        @self.registry.action(
            "Scroll down the page by pixel amount - if no amount is specified, scroll by one page",
            param_model=ScrollAction,
        )
        async def scroll_down(params: ScrollAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            amount = params.amount if params.amount and params.amount > 0 else 600
            await browser.execute_action("SCROLL", {
                "direction": "DOWN",
                "amount": amount,
                "tabId": browser._current_tab_id,
            })
            browser.invalidate_state()
            msg = f"📜  Scrolled down by {amount} pixels"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action(
            "Scroll up the page by pixel amount - if no amount is specified, scroll by one page",
            param_model=ScrollAction,
        )
        async def scroll_up(params: ScrollAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            amount = params.amount if params.amount and params.amount > 0 else 600
            await browser.execute_action("SCROLL", {
                "direction": "UP",
                "amount": amount,
                "tabId": browser._current_tab_id,
            })
            browser.invalidate_state()
            msg = f"📜  Scrolled up by {amount} pixels"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)

        @self.registry.action(
            "Send keys to current focused element or page",
            param_model=SendKeysAction,
        )
        async def send_keys(params: SendKeysAction, browser: ExtensionBrowserContext):
            if browser._current_tab_id is None:
                return ActionResult(error="No agent-owned browser tabs remain open. Please open a new tab.", include_in_memory=True)

            await browser.execute_action("PRESS_KEY", {
                "key": params.keys,
                "tabId": browser._current_tab_id,
            })
            browser.invalidate_state()
            msg = f"⌨️  Sent keys: {params.keys}"
            logger.info(msg)
            return ActionResult(extracted_content=msg, include_in_memory=True)
