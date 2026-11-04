import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

import gradio as gr

# from browser_use.agent.service import Agent
from browser_use.agent.views import (
    AgentHistoryList,
    AgentOutput,
)
from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from browser_use.browser.views import BrowserState
from gradio.components import Component
from langchain_core.language_models.chat_models import BaseChatModel

import httpx
from src.agent.browser_use.browser_use_agent import BrowserUseAgent
from src.browser.extension_browser import ExtensionBrowser
from src.browser.extension_context import ExtensionBrowserContext
from src.controller.extension_controller import ExtensionController
from src.utils import llm_provider
from src.webui.webui_manager import WebuiManager

logger = logging.getLogger(__name__)


# --- Helper Functions --- (Defined at module level)


async def _initialize_llm(
        provider: Optional[str],
        model_name: Optional[str],
        temperature: float,
        base_url: Optional[str],
        api_key: Optional[str],
        num_ctx: Optional[int] = None,
) -> Optional[BaseChatModel]:
    """Initializes the LLM based on settings. Returns None if provider/model is missing."""
    if not provider or not model_name:
        logger.info("LLM Provider or Model Name not specified, LLM will be None.")
        return None
    try:
        # Use your actual LLM provider logic here
        logger.info(
            f"Initializing LLM: Provider={provider}, Model={model_name}, Temp={temperature}"
        )
        # Example using a placeholder function
        llm = llm_provider.get_llm_model(
            provider=provider,
            model_name=model_name,
            temperature=temperature,
            base_url=base_url or None,
            api_key=api_key or None,
            # Add other relevant params like num_ctx for ollama
            num_ctx=num_ctx if provider == "ollama" else None,
        )
        return llm
    except Exception as e:
        logger.error(f"Failed to initialize LLM: {e}", exc_info=True)
        gr.Warning(
            f"Failed to initialize LLM '{model_name}' for provider '{provider}'. Please check settings. Error: {e}"
        )
        return None


def _get_config_value(
        webui_manager: WebuiManager,
        comp_dict: Dict[gr.components.Component, Any],
        comp_id_suffix: str,
        default: Any = None,
) -> Any:
    """Safely get value from component dictionary using its ID suffix relative to the tab."""
    # Assumes component ID format is "tab_name.comp_name"
    tab_name = "browser_use_agent"  # Hardcode or derive if needed
    comp_id = f"{tab_name}.{comp_id_suffix}"
    # Need to find the component object first using the ID from the manager
    try:
        comp = webui_manager.get_component_by_id(comp_id)
        return comp_dict.get(comp, default)
    except KeyError:
        # Try accessing settings tabs as well
        for prefix in ["agent_settings", "browser_settings"]:
            try:
                comp_id = f"{prefix}.{comp_id_suffix}"
                comp = webui_manager.get_component_by_id(comp_id)
                return comp_dict.get(comp, default)
            except KeyError:
                continue
        logger.warning(
            f"Component with suffix '{comp_id_suffix}' not found in manager for value lookup."
        )
        return default


def _format_agent_output(model_output: AgentOutput) -> str:
    """Formats AgentOutput for display in the chatbot using JSON."""
    content = ""
    if model_output:
        try:
            # Directly use model_dump if actions and current_state are Pydantic models
            action_dump = [
                action.model_dump(exclude_none=True) for action in model_output.action
            ]

            state_dump = model_output.current_state.model_dump(exclude_none=True)
            model_output_dump = {
                "current_state": state_dump,
                "action": action_dump,
            }
            # Dump to JSON string with indentation
            json_string = json.dumps(model_output_dump, indent=4, ensure_ascii=False)
            # Wrap in <pre><code> for proper display in HTML
            content = f"<pre><code class='language-json'>{json_string}</code></pre>"

        except AttributeError as ae:
            logger.error(
                f"AttributeError during model dump: {ae}. Check if 'action' or 'current_state' or their items support 'model_dump'."
            )
            content = f"<pre><code>Error: Could not format agent output (AttributeError: {ae}).\nRaw output: {str(model_output)}</code></pre>"
        except Exception as e:
            logger.error(f"Error formatting agent output: {e}", exc_info=True)
            # Fallback to simple string representation on error
            content = f"<pre><code>Error formatting agent output.\nRaw output:\n{str(model_output)}</code></pre>"

    return content.strip()


# --- Updated Callback Implementation ---


async def _handle_new_step(
        webui_manager: WebuiManager, state: BrowserState, output: AgentOutput, step_num: int
):
    """Callback for each step taken by the agent, including screenshot display."""

    # Use the correct chat history attribute name from the user's code
    if not hasattr(webui_manager, "bu_chat_history"):
        logger.error(
            "Attribute 'bu_chat_history' not found in webui_manager! Cannot add chat message."
        )
        # Initialize it maybe? Or raise an error? For now, log and potentially skip chat update.
        webui_manager.bu_chat_history = []  # Initialize if missing (consider if this is the right place)
        # return # Or stop if this is critical
    step_num -= 1
    logger.info(f"Step {step_num} completed.")

    # --- Screenshot Handling ---
    screenshot_html = ""
    # Ensure state.screenshot exists and is not empty before proceeding
    # Use getattr for safer access
    screenshot_data = getattr(state, "screenshot", None)
    if screenshot_data:
        try:
            # Basic validation: check if it looks like base64
            if (
                    isinstance(screenshot_data, str) and len(screenshot_data) > 100
            ):  # Arbitrary length check
                # *** UPDATED STYLE: Removed centering, adjusted width ***
                img_tag = f'<img src="data:image/jpeg;base64,{screenshot_data}" alt="Step {step_num} Screenshot" style="max-width: 800px; max-height: 600px; object-fit:contain;" />'
                screenshot_html = (
                        img_tag + "<br/>"
                )  # Use <br/> for line break after inline-block image
            else:
                logger.warning(
                    f"Screenshot for step {step_num} seems invalid (type: {type(screenshot_data)}, len: {len(screenshot_data) if isinstance(screenshot_data, str) else 'N/A'})."
                )
                screenshot_html = "**[Invalid screenshot data]**<br/>"

        except Exception as e:
            logger.error(
                f"Error processing or formatting screenshot for step {step_num}: {e}",
                exc_info=True,
            )
            screenshot_html = "**[Error displaying screenshot]**<br/>"
    else:
        logger.debug(f"No screenshot available for step {step_num}.")

    # --- Format Agent Output ---
    formatted_output = _format_agent_output(output)  # Use the updated function

    # --- Combine and Append to Chat ---
    step_header = f"--- **Step {step_num}** ---"
    # Combine header, image (with line break), and JSON block
    final_content = step_header + "<br/>" + screenshot_html + formatted_output

    chat_message = {
        "role": "assistant",
        "content": final_content.strip(),  # Remove leading/trailing whitespace
    }

    # Append to the correct chat history list
    webui_manager.bu_chat_history.append(chat_message)

    await asyncio.sleep(0.05)


def _handle_done(webui_manager: WebuiManager, history: AgentHistoryList):
    """Callback when the agent finishes the task (success or failure)."""
    logger.info(
        f"Agent task finished. Duration: {history.total_duration_seconds():.2f}s, Tokens: {history.total_input_tokens()}"
    )
    final_summary = "**Task Completed**\n"
    final_summary += f"- Duration: {history.total_duration_seconds():.2f} seconds\n"
    final_summary += f"- Total Input Tokens: {history.total_input_tokens()}\n"  # Or total tokens if available

    final_result = history.final_result()
    if final_result:
        final_summary += f"- Final Result: {final_result}\n"

    errors = history.errors()
    if errors and any(errors):
        final_summary += f"- **Errors:**\n```\n{errors}\n```\n"
    else:
        final_summary += "- Status: Success\n"

    webui_manager.bu_chat_history.append(
        {"role": "assistant", "content": final_summary}
    )


async def _ask_assistant_callback(
        webui_manager: WebuiManager, query: str, browser_context: Any
) -> Dict[str, Any]:
    """Callback triggered by the agent's ask_for_assistant action."""
    logger.info("Agent requires assistance. Waiting for user input.")

    if not hasattr(webui_manager, "_chat_history"):
        logger.error("Chat history not found in webui_manager during ask_assistant!")
        return {"response": "Internal Error: Cannot display help request."}

    webui_manager.bu_chat_history.append(
        {
            "role": "assistant",
            "content": f"**Need Help:** {query}\nPlease provide information or perform the required action in the browser, then type your response/confirmation below and click 'Submit Response'.",
        }
    )

    # Use state stored in webui_manager
    webui_manager.bu_response_event = asyncio.Event()
    webui_manager.bu_user_help_response = None  # Reset previous response

    try:
        logger.info("Waiting for user response event...")
        await asyncio.wait_for(
            webui_manager.bu_response_event.wait(), timeout=3600.0
        )  # Long timeout
        logger.info("User response event received.")
    except asyncio.TimeoutError:
        logger.warning("Timeout waiting for user assistance.")
        webui_manager.bu_chat_history.append(
            {
                "role": "assistant",
                "content": "**Timeout:** No response received. Trying to proceed.",
            }
        )
        webui_manager.bu_response_event = None  # Clear the event
        return {"response": "Timeout: User did not respond."}  # Inform the agent

    response = webui_manager.bu_user_help_response
    webui_manager.bu_chat_history.append(
        {"role": "user", "content": response}
    )  # Show user response in chat
    webui_manager.bu_response_event = (
        None  # Clear the event for the next potential request
    )
    return {"response": response}


# --- Core Agent Execution Logic --- (Needs access to webui_manager)


async def run_agent_task(
        webui_manager: WebuiManager, components: Dict[gr.components.Component, Any]
) -> AsyncGenerator[Dict[gr.components.Component, Any], None]:
    """Handles the entire lifecycle of initializing and running the agent."""

    # --- Get Components ---
    # Need handles to specific UI components to update them
    user_input_comp = webui_manager.get_component_by_id("browser_use_agent.user_input")
    run_button_comp = webui_manager.get_component_by_id("browser_use_agent.run_button")
    stop_button_comp = webui_manager.get_component_by_id(
        "browser_use_agent.stop_button"
    )
    pause_resume_button_comp = webui_manager.get_component_by_id(
        "browser_use_agent.pause_resume_button"
    )
    clear_button_comp = webui_manager.get_component_by_id(
        "browser_use_agent.clear_button"
    )
    chatbot_comp = webui_manager.get_component_by_id("browser_use_agent.chatbot")
    history_file_comp = webui_manager.get_component_by_id(
        "browser_use_agent.agent_history_file"
    )
    gif_comp = webui_manager.get_component_by_id("browser_use_agent.recording_gif")
    browser_view_comp = webui_manager.get_component_by_id(
        "browser_use_agent.browser_view"
    )

    # --- 1. Get Task and Initial UI Update ---
    task = components.get(user_input_comp, "").strip()
    if not task:
        gr.Warning("Please enter a task.")
        yield {run_button_comp: gr.update(interactive=True)}
        return

    # Set running state indirectly via _current_task
    webui_manager.bu_chat_history.append({"role": "user", "content": task})

    yield {
        user_input_comp: gr.Textbox(
            value="", interactive=False, placeholder="Agent is running..."
        ),
        run_button_comp: gr.Button(value="⏳ Running...", interactive=False),
        stop_button_comp: gr.Button(interactive=True),
        pause_resume_button_comp: gr.Button(value="⏸️ Pause", interactive=True),
        clear_button_comp: gr.Button(interactive=False),
        chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
        history_file_comp: gr.update(value=None),
        gif_comp: gr.update(value=None),
    }

    # --- Pre-flight Check: FastAPI health & Chrome Extension connection ---
    backend_url = os.getenv("EXTENSION_SERVER_URL", "http://127.0.0.1:8765").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as health_client:
            health_resp = await health_client.get(f"{backend_url}/health")
            if health_resp.status_code != 200:
                err_msg = f"FastAPI backend health check failed with HTTP {health_resp.status_code}."
                logger.error(err_msg)
                gr.Error(err_msg)
                webui_manager.bu_chat_history.append({"role": "assistant", "content": f"**Connection Error:** {err_msg}"})
                yield {
                    user_input_comp: gr.update(interactive=True, placeholder="Enter task..."),
                    run_button_comp: gr.update(value="▶️ Submit Task", interactive=True),
                    stop_button_comp: gr.update(value="⏹️ Stop", interactive=False),
                    pause_resume_button_comp: gr.update(value="⏸️ Pause", interactive=False),
                    clear_button_comp: gr.update(interactive=True),
                    chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
                }
                return

            health_data = health_resp.json()
            if not health_data.get("extension_connected", False):
                err_msg = (
                    "Chrome Extension is not connected to FastAPI backend. "
                    "Please ensure Google Chrome is open with the AI Kings extension active."
                )
                logger.error(err_msg)
                gr.Error(err_msg)
                webui_manager.bu_chat_history.append({"role": "assistant", "content": f"**Connection Error:** {err_msg}"})
                yield {
                    user_input_comp: gr.update(interactive=True, placeholder="Enter task..."),
                    run_button_comp: gr.update(value="▶️ Submit Task", interactive=True),
                    stop_button_comp: gr.update(value="⏹️ Stop", interactive=False),
                    pause_resume_button_comp: gr.update(value="⏸️ Pause", interactive=False),
                    clear_button_comp: gr.update(interactive=True),
                    chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
                }
                return
    except Exception as e:
        err_msg = (
            f"Cannot reach FastAPI server at {backend_url}. "
            "Please ensure the backend server is running."
        )
        logger.error(f"{err_msg}: {e}")
        gr.Error(err_msg)
        webui_manager.bu_chat_history.append({"role": "assistant", "content": f"**Connection Error:** {err_msg}"})
        yield {
            user_input_comp: gr.update(interactive=True, placeholder="Enter task..."),
            run_button_comp: gr.update(value="▶️ Submit Task", interactive=True),
            stop_button_comp: gr.update(value="⏹️ Stop", interactive=False),
            pause_resume_button_comp: gr.update(value="⏸️ Pause", interactive=False),
            clear_button_comp: gr.update(interactive=True),
            chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
        }
        return

    # --- Agent Settings ---
    # Access settings values via components dict, getting IDs from webui_manager
    def get_setting(key, default=None):
        comp = webui_manager.id_to_component.get(f"agent_settings.{key}")
        return components.get(comp, default) if comp else default

    override_system_prompt = get_setting("override_system_prompt") or None
    extend_system_prompt = get_setting("extend_system_prompt") or None
    llm_provider_name = get_setting(
        "llm_provider", None
    )  # Default to None if not found
    llm_model_name = get_setting("llm_model_name", None)
    llm_temperature = get_setting("llm_temperature", 0.6)
    use_vision = False  # Screenshots not supported in Extension architecture
    ollama_num_ctx = get_setting("ollama_num_ctx", 16000)
    llm_base_url = get_setting("llm_base_url") or None
    llm_api_key = get_setting("llm_api_key") or None
    max_steps = get_setting("max_steps", 100)
    max_actions = get_setting("max_actions", 10)
    max_input_tokens = get_setting("max_input_tokens", 128000)
    tool_calling_str = get_setting("tool_calling_method", "auto")
    tool_calling_method = tool_calling_str if tool_calling_str != "None" else None
    mcp_server_config_comp = webui_manager.id_to_component.get(
        "agent_settings.mcp_server_config"
    )
    mcp_server_config_str = (
        components.get(mcp_server_config_comp) if mcp_server_config_comp else None
    )
    mcp_server_config = (
        json.loads(mcp_server_config_str) if mcp_server_config_str else None
    )

    # Planner LLM Settings (Optional)
    planner_llm_provider_name = get_setting("planner_llm_provider") or None
    planner_llm = None
    planner_use_vision = False
    if planner_llm_provider_name:
        planner_llm_model_name = get_setting("planner_llm_model_name")
        planner_llm_temperature = get_setting("planner_llm_temperature", 0.6)
        planner_ollama_num_ctx = get_setting("planner_ollama_num_ctx", 16000)
        planner_llm_base_url = get_setting("planner_llm_base_url") or None
        planner_llm_api_key = get_setting("planner_llm_api_key") or None
        planner_use_vision = False  # Screenshots not supported in Extension architecture

        planner_llm = await _initialize_llm(
            planner_llm_provider_name,
            planner_llm_model_name,
            planner_llm_temperature,
            planner_llm_base_url,
            planner_llm_api_key,
            planner_ollama_num_ctx if planner_llm_provider_name == "ollama" else None,
        )

    # --- Browser Settings ---
    def get_browser_setting(key, default=None):
        comp = webui_manager.id_to_component.get(f"browser_settings.{key}")
        return components.get(comp, default) if comp else default

    keep_browser_open = get_browser_setting("keep_browser_open", True)
    window_w = int(get_browser_setting("window_w", 1280))
    window_h = int(get_browser_setting("window_h", 1100))
    save_agent_history_path = get_browser_setting(
        "save_agent_history_path", "./tmp/agent_history"
    )
    save_download_path = get_browser_setting("save_download_path", "./tmp/downloads")

    os.makedirs(save_agent_history_path, exist_ok=True)
    if save_download_path:
        os.makedirs(save_download_path, exist_ok=True)

    # --- 2. Initialize LLM ---
    main_llm = await _initialize_llm(
        llm_provider_name,
        llm_model_name,
        llm_temperature,
        llm_base_url,
        llm_api_key,
        ollama_num_ctx if llm_provider_name == "ollama" else None,
    )
    if not main_llm:
        raise ValueError(f"Failed to initialize LLM '{llm_model_name}' for provider '{llm_provider_name}'. Please check your API key and model settings.")

    # Pass the webui_manager instance to the callback when wrapping it
    async def ask_callback_wrapper(
            query: str, browser_context: Any
    ) -> Dict[str, Any]:
        return await _ask_assistant_callback(webui_manager, query, browser_context)

    # --- 3. Close Legacy/Non-Extension Resources ---
    if webui_manager.bu_browser and not isinstance(webui_manager.bu_browser, ExtensionBrowser):
        logger.info("Closing previous non-extension browser.")
        try:
            await webui_manager.bu_browser.close()
        except Exception as e:
            logger.warning(f"Error closing legacy browser: {e}")
        webui_manager.bu_browser = None

    if webui_manager.bu_browser_context and not isinstance(webui_manager.bu_browser_context, ExtensionBrowserContext):
        logger.info("Closing previous non-extension browser context.")
        try:
            await webui_manager.bu_browser_context.close()
        except Exception as e:
            logger.warning(f"Error closing legacy context: {e}")
        webui_manager.bu_browser_context = None

    # Close previous extension resources if not keeping open
    if not keep_browser_open:
        if webui_manager.bu_browser_context:
            logger.info("Closing previous extension browser context.")
            try:
                await webui_manager.bu_browser_context.close()
            except Exception as e:
                logger.warning(f"Error closing previous extension context: {e}")
            webui_manager.bu_browser_context = None
        if webui_manager.bu_browser:
            logger.info("Closing previous extension browser.")
            try:
                await webui_manager.bu_browser.close()
            except Exception as e:
                logger.warning(f"Error closing previous extension browser: {e}")
            webui_manager.bu_browser = None

    # --- 4. Initialize Extension Controller, Browser and Context ---
    if not webui_manager.bu_controller or not isinstance(webui_manager.bu_controller, ExtensionController):
        webui_manager.bu_controller = ExtensionController(
            ask_assistant_callback=ask_callback_wrapper
        )
        await webui_manager.bu_controller.setup_mcp_client(mcp_server_config)

    should_close_browser_on_finish = not keep_browser_open

    try:
        if not webui_manager.bu_browser:
            logger.info("Initializing ExtensionBrowser instance.")
            webui_manager.bu_browser = ExtensionBrowser()

        # If previous context is marked closed, discard it so a new context is created
        if webui_manager.bu_browser_context and getattr(webui_manager.bu_browser_context, "is_closed", False):
            logger.info("Previous extension browser context is closed. Discarding reference.")
            webui_manager.bu_browser_context = None

        if not webui_manager.bu_browser_context:
            logger.info("Initializing ExtensionBrowserContext instance.")
            webui_manager.bu_browser_context = ExtensionBrowserContext(
                server_url=backend_url,
            )

        # --- 5. Initialize or Update Agent ---
        webui_manager.bu_agent_task_id = str(uuid.uuid4())  # New ID for this task run
        os.makedirs(
            os.path.join(save_agent_history_path, webui_manager.bu_agent_task_id),
            exist_ok=True,
        )
        history_file = os.path.join(
            save_agent_history_path,
            webui_manager.bu_agent_task_id,
            f"{webui_manager.bu_agent_task_id}.json",
        )
        gif_path = os.path.join(
            save_agent_history_path,
            webui_manager.bu_agent_task_id,
            f"{webui_manager.bu_agent_task_id}.gif",
        )

        # Pass the webui_manager to callbacks when wrapping them
        async def step_callback_wrapper(
                state: BrowserState, output: AgentOutput, step_num: int
        ):
            await _handle_new_step(webui_manager, state, output, step_num)

        def done_callback_wrapper(history: AgentHistoryList):
            _handle_done(webui_manager, history)

        if not webui_manager.bu_agent:
            logger.info(f"Initializing new agent for task: {task}")
            if not webui_manager.bu_browser or not webui_manager.bu_browser_context:
                raise ValueError(
                    "Browser or Context not initialized, cannot create agent."
                )
            webui_manager.bu_agent = BrowserUseAgent(
                task=task,
                llm=main_llm,
                browser=webui_manager.bu_browser,
                browser_context=webui_manager.bu_browser_context,
                controller=webui_manager.bu_controller,
                register_new_step_callback=step_callback_wrapper,
                register_done_callback=done_callback_wrapper,
                use_vision=False,
                override_system_message=override_system_prompt,
                extend_system_message=extend_system_prompt,
                max_input_tokens=max_input_tokens,
                max_actions_per_step=max_actions,
                tool_calling_method=tool_calling_method,
                planner_llm=planner_llm,
                use_vision_for_planner=False,
                source="webui",
                use_extension=True,
            )
            webui_manager.bu_agent.state.agent_id = webui_manager.bu_agent_task_id
            webui_manager.bu_agent.settings.generate_gif = False
        else:
            webui_manager.bu_agent.state.agent_id = webui_manager.bu_agent_task_id
            webui_manager.bu_agent.add_new_task(task)
            webui_manager.bu_agent.settings.generate_gif = False
            webui_manager.bu_agent.browser = webui_manager.bu_browser
            webui_manager.bu_agent.browser_context = webui_manager.bu_browser_context
            webui_manager.bu_agent.controller = webui_manager.bu_controller

        # Hold local references so background operations / Clear clicks cannot cause
        # NoneType attribute errors if webui_manager references are cleared concurrently.
        local_agent = webui_manager.bu_agent
        local_context = webui_manager.bu_browser_context
        local_browser = webui_manager.bu_browser
        local_controller = webui_manager.bu_controller

        # --- 6. Run Agent Task and Stream Updates ---
        agent_run_coro = local_agent.run(max_steps=max_steps)
        agent_task = asyncio.create_task(agent_run_coro)
        webui_manager.bu_current_task = agent_task  # Store the task

        last_chat_len = len(webui_manager.bu_chat_history)
        while not agent_task.done():
            is_paused = getattr(getattr(local_agent, "state", None), "paused", False)
            is_stopped = getattr(getattr(local_agent, "state", None), "stopped", False)

            # Check for pause state
            if is_paused:
                yield {
                    pause_resume_button_comp: gr.update(
                        value="▶️ Resume", interactive=True
                    ),
                    stop_button_comp: gr.update(interactive=True),
                }
                # Wait until pause is released or task is stopped/done
                while is_paused and not agent_task.done():
                    # Re-check agent state in loop safely
                    is_paused = getattr(getattr(local_agent, "state", None), "paused", False)
                    is_stopped = getattr(getattr(local_agent, "state", None), "stopped", False)
                    if is_stopped:  # Stop signal received while paused
                        break
                    await asyncio.sleep(0.2)

                if (
                        agent_task.done() or is_stopped
                ):  # If stopped or task finished while paused
                    break

                # If resumed, yield UI update
                yield {
                    pause_resume_button_comp: gr.update(
                        value="⏸️ Pause", interactive=True
                    ),
                    run_button_comp: gr.update(
                        value="⏳ Running...", interactive=False
                    ),
                }

            # Check if agent stopped itself or stop button was pressed (which sets agent.state.stopped)
            if is_stopped:
                logger.info("Agent has stopped (internally or via stop button).")
                if not agent_task.done():
                    # Ensure the task coroutine finishes if agent just set flag
                    try:
                        await asyncio.wait_for(
                            agent_task, timeout=1.0
                        )  # Give it a moment to exit run()
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Agent task did not finish quickly after stop signal, cancelling."
                        )
                        agent_task.cancel()
                    except Exception:  # Catch task exceptions if it errors on stop
                        pass
                break  # Exit the streaming loop

            # Check if agent is asking for help (via response_event)
            update_dict = {}
            if webui_manager.bu_response_event is not None:
                update_dict = {
                    user_input_comp: gr.update(
                        placeholder="Agent needs help. Enter response and submit.",
                        interactive=True,
                    ),
                    run_button_comp: gr.update(
                        value="✔️ Submit Response", interactive=True
                    ),
                    pause_resume_button_comp: gr.update(interactive=False),
                    stop_button_comp: gr.update(interactive=False),
                    chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
                }
                last_chat_len = len(webui_manager.bu_chat_history)
                yield update_dict
                # Wait until response is submitted or task finishes
                await webui_manager.bu_response_event.wait()

                # Restore UI after response submitted or if task ended unexpectedly
                if not agent_task.done():
                    yield {
                        user_input_comp: gr.update(
                            placeholder="Agent is running...", interactive=False
                        ),
                        run_button_comp: gr.update(
                            value="⏳ Running...", interactive=False
                        ),
                        pause_resume_button_comp: gr.update(interactive=True),
                        stop_button_comp: gr.update(interactive=True),
                    }
                else:
                    break  # Task finished while waiting for response

            # Update Chatbot if new messages arrived via callbacks
            if len(webui_manager.bu_chat_history) > last_chat_len:
                update_dict[chatbot_comp] = gr.update(
                    value=webui_manager.bu_chat_history
                )
                last_chat_len = len(webui_manager.bu_chat_history)

            # Update Browser View (not supported in Extension mode)
            update_dict[browser_view_comp] = gr.update(visible=False)

            # Yield accumulated updates
            if update_dict:
                yield update_dict

            await asyncio.sleep(0.1)  # Polling interval

        # --- 7. Task Finalization ---
        if local_agent and hasattr(local_agent, "state") and local_agent.state:
            local_agent.state.paused = False
            local_agent.state.stopped = False
        final_update = {}
        try:
            logger.info("Agent task completing...")
            # Await the task ensure completion and catch exceptions if not already caught
            if not agent_task.done():
                await agent_task  # Retrieve result/exception
            elif agent_task.cancelled():
                logger.info("Agent task was cancelled.")
            elif agent_task.exception():  # Check if task finished with exception
                agent_task.result()  # Raise the exception to be caught below
            logger.info("Agent task completed processing.")

            if local_agent and hasattr(local_agent, "save_history"):
                logger.info(f"Explicitly saving agent history to: {history_file}")
                local_agent.save_history(history_file)

            if os.path.exists(history_file):
                final_update[history_file_comp] = gr.File(value=history_file)

            if gif_path and os.path.exists(gif_path):
                logger.info(f"GIF found at: {gif_path}")
                final_update[gif_comp] = gr.Image(value=gif_path)

        except asyncio.CancelledError:
            logger.info("Agent task was cancelled.")
            if not any(
                    "Cancelled" in msg.get("content", "")
                    for msg in webui_manager.bu_chat_history
                    if msg.get("role") == "assistant"
            ):
                webui_manager.bu_chat_history.append(
                    {"role": "assistant", "content": "**Task Cancelled**."}
                )
            final_update[chatbot_comp] = gr.update(value=webui_manager.bu_chat_history)
        except Exception as e:
            logger.error(f"Error during agent execution: {e}", exc_info=True)
            error_message = (
                f"**Agent Execution Error:**\n```\n{type(e).__name__}: {e}\n```"
            )
            if not any(
                    error_message in msg.get("content", "")
                    for msg in webui_manager.bu_chat_history
                    if msg.get("role") == "assistant"
            ):
                webui_manager.bu_chat_history.append(
                    {"role": "assistant", "content": error_message}
                )
            final_update[chatbot_comp] = gr.update(value=webui_manager.bu_chat_history)
            gr.Error(f"Agent execution failed: {e}")

        finally:
            webui_manager.bu_current_task = None  # Clear the task reference
            webui_manager.bu_is_running = False

            # Close browser/context if requested
            if should_close_browser_on_finish:
                if local_context:
                    logger.info("Closing browser context after task.")
                    try:
                        await local_context.close()
                    except Exception as e:
                        logger.error(f"Error closing browser context: {e}")
                    if webui_manager.bu_browser_context is local_context:
                        webui_manager.bu_browser_context = None
                if local_browser:
                    logger.info("Closing browser after task.")
                    try:
                        await local_browser.close()
                    except Exception as e:
                        logger.error(f"Error closing browser: {e}")
                    if webui_manager.bu_browser is local_browser:
                        webui_manager.bu_browser = None

            # --- 8. Final UI Update ---
            final_update.update(
                {
                    user_input_comp: gr.update(
                        value="",
                        interactive=True,
                        placeholder="Enter your next task...",
                    ),
                    run_button_comp: gr.update(value="▶️ Submit Task", interactive=True),
                    stop_button_comp: gr.update(value="⏹️ Stop", interactive=False),
                    pause_resume_button_comp: gr.update(
                        value="⏸️ Pause", interactive=False
                    ),
                    clear_button_comp: gr.update(interactive=True),
                    # Ensure final chat history is shown
                    chatbot_comp: gr.update(value=webui_manager.bu_chat_history),
                }
            )
            yield final_update

    except Exception as e:
        # Catch errors during setup (before agent run starts)
        logger.error(f"Error setting up agent task: {e}", exc_info=True)
        webui_manager.bu_current_task = None  # Ensure state is reset
        webui_manager.bu_is_running = False
        yield {
            user_input_comp: gr.update(
                interactive=True, placeholder="Error during setup. Enter task..."
            ),
            run_button_comp: gr.update(value="▶️ Submit Task", interactive=True),
            stop_button_comp: gr.update(value="⏹️ Stop", interactive=False),
            pause_resume_button_comp: gr.update(value="⏸️ Pause", interactive=False),
            clear_button_comp: gr.update(interactive=True),
            chatbot_comp: gr.update(
                value=webui_manager.bu_chat_history
                      + [{"role": "assistant", "content": f"**Setup Error:** {e}"}]
            ),
        }
    finally:
        webui_manager.bu_is_running = False


# --- Button Click Handlers --- (Need access to webui_manager)


async def handle_submit(
        webui_manager: WebuiManager, components: Dict[gr.components.Component, Any]
):
    """Handles clicks on the main 'Submit' button."""
    user_input_comp = webui_manager.get_component_by_id("browser_use_agent.user_input")
    user_input_value = components.get(user_input_comp, "").strip()

    # Check if waiting for user assistance
    if webui_manager.bu_response_event and not webui_manager.bu_response_event.is_set():
        logger.info(f"User submitted assistance: {user_input_value}")
        webui_manager.bu_user_help_response = (
            user_input_value if user_input_value else "User provided no text response."
        )
        webui_manager.bu_response_event.set()
        # UI updates handled by the main loop reacting to the event being set
        yield {
            user_input_comp: gr.update(
                value="",
                interactive=False,
                placeholder="Waiting for agent to continue...",
            ),
            webui_manager.get_component_by_id(
                "browser_use_agent.run_button"
            ): gr.update(value="⏳ Running...", interactive=False),
        }
    # Check if a task is currently running (concurrency guard)
    elif (
        webui_manager.bu_is_running
        or (webui_manager.bu_current_task and not webui_manager.bu_current_task.done())
    ):
        logger.warning(
            "Submit button clicked while agent is already running and not asking for help."
        )
        gr.Info("Agent is currently running. Please wait or use Stop/Pause.")
        yield {}  # Block concurrent execution
        return
    else:
        # Handle submission for a new task
        logger.info("Submit button clicked for new task.")
        webui_manager.bu_is_running = True
        try:
            # Use async generator to stream updates from run_agent_task
            async for update in run_agent_task(webui_manager, components):
                yield update
        finally:
            webui_manager.bu_is_running = False


async def handle_stop(webui_manager: WebuiManager):
    """Handles clicks on the 'Stop' button."""
    logger.info("Stop button clicked.")
    agent = webui_manager.bu_agent
    task = webui_manager.bu_current_task

    if (agent or task) and (task and not task.done()):
        # Signal the agent to stop by setting its internal flag
        if agent and hasattr(agent, "state") and agent.state:
            agent.state.stopped = True
            agent.state.paused = False  # Ensure not paused if stopped
        return {
            webui_manager.get_component_by_id(
                "browser_use_agent.stop_button"
            ): gr.update(interactive=False, value="⏹️ Stopping..."),
            webui_manager.get_component_by_id(
                "browser_use_agent.pause_resume_button"
            ): gr.update(interactive=False),
            webui_manager.get_component_by_id(
                "browser_use_agent.run_button"
            ): gr.update(interactive=False),
        }
    else:
        logger.warning("Stop clicked but agent is not running or task is already done.")
        # Reset UI just in case it's stuck
        return {
            webui_manager.get_component_by_id(
                "browser_use_agent.run_button"
            ): gr.update(interactive=True),
            webui_manager.get_component_by_id(
                "browser_use_agent.stop_button"
            ): gr.update(interactive=False),
            webui_manager.get_component_by_id(
                "browser_use_agent.pause_resume_button"
            ): gr.update(interactive=False),
            webui_manager.get_component_by_id(
                "browser_use_agent.clear_button"
            ): gr.update(interactive=True),
        }


async def handle_pause_resume(webui_manager: WebuiManager):
    """Handles clicks on the 'Pause/Resume' button."""
    agent = webui_manager.bu_agent
    task = webui_manager.bu_current_task

    if agent and task and not task.done() and hasattr(agent, "state") and agent.state:
        if agent.state.paused:
            logger.info("Resume button clicked.")
            agent.resume()
            # UI update happens in main loop
            return {
                webui_manager.get_component_by_id(
                    "browser_use_agent.pause_resume_button"
                ): gr.update(value="⏸️ Pause", interactive=True)
            }  # Optimistic update
        else:
            logger.info("Pause button clicked.")
            agent.pause()
            return {
                webui_manager.get_component_by_id(
                    "browser_use_agent.pause_resume_button"
                ): gr.update(value="▶️ Resume", interactive=True)
            }  # Optimistic update
    else:
        logger.warning(
            "Pause/Resume clicked but agent is not running or doesn't support state."
        )
        return {}  # No change


async def handle_clear(webui_manager: WebuiManager):
    """Handles clicks on the 'Clear' button."""
    logger.info("Clear button clicked.")

    # 1. Stop any running task first
    agent = webui_manager.bu_agent
    task = webui_manager.bu_current_task
    if agent and hasattr(agent, "state") and agent.state:
        try:
            agent.stop()
        except Exception as e:
            logger.warning(f"Error stopping agent on clear: {e}")

    if task and not task.done():
        logger.info("Clearing requires stopping the current task.")
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)  # Wait briefly
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as e:
            logger.warning(f"Error stopping task on clear: {e}")

    webui_manager.bu_current_task = None
    webui_manager.bu_is_running = False

    # 2. Close MCP client and clear controller
    if webui_manager.bu_controller:
        try:
            await webui_manager.bu_controller.close_mcp_client()
        except Exception as e:
            logger.warning(f"Error closing MCP client on clear: {e}")
        webui_manager.bu_controller = None

    # 3. Clean up browser context and agent-owned tabs
    if webui_manager.bu_browser_context:
        logger.info("Closing browser context and agent tabs on clear.")
        try:
            await webui_manager.bu_browser_context.close()
        except Exception as e:
            logger.warning(f"Error closing browser context on clear: {e}")
        webui_manager.bu_browser_context = None

    # 4. Clean up browser
    if webui_manager.bu_browser:
        logger.info("Closing browser on clear.")
        try:
            await webui_manager.bu_browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser on clear: {e}")
        webui_manager.bu_browser = None

    webui_manager.bu_agent = None

    # 5. Reset state stored in manager
    webui_manager.bu_chat_history = []
    webui_manager.bu_response_event = None
    webui_manager.bu_user_help_response = None
    webui_manager.bu_agent_task_id = None

    logger.info("Agent state and browser resources cleared.")

    # Reset UI components
    return {
        webui_manager.get_component_by_id("browser_use_agent.chatbot"): gr.update(
            value=[]
        ),
        webui_manager.get_component_by_id("browser_use_agent.user_input"): gr.update(
            value="", placeholder="Enter your task here..."
        ),
        webui_manager.get_component_by_id(
            "browser_use_agent.agent_history_file"
        ): gr.update(value=None),
        webui_manager.get_component_by_id("browser_use_agent.recording_gif"): gr.update(
            value=None
        ),
        webui_manager.get_component_by_id("browser_use_agent.browser_view"): gr.update(
            value="<div style='...'>Browser Cleared</div>"
        ),
        webui_manager.get_component_by_id("browser_use_agent.run_button"): gr.update(
            value="▶️ Submit Task", interactive=True
        ),
        webui_manager.get_component_by_id("browser_use_agent.stop_button"): gr.update(
            interactive=False
        ),
        webui_manager.get_component_by_id(
            "browser_use_agent.pause_resume_button"
        ): gr.update(value="⏸️ Pause", interactive=False),
        webui_manager.get_component_by_id("browser_use_agent.clear_button"): gr.update(
            interactive=True
        ),
    }


# --- Tab Creation Function ---


def create_browser_use_agent_tab(webui_manager: WebuiManager):
    """
    Create the run agent tab, defining UI, state, and handlers.
    """
    webui_manager.init_browser_use_agent()

    # --- Define UI Components ---
    tab_components = {}
    with gr.Column():
        chatbot = gr.Chatbot(
            lambda: webui_manager.bu_chat_history,  # Load history dynamically
            elem_id="browser_use_chatbot",
            label="Agent Interaction",
            type="messages",
            height=600,
            show_copy_button=True,
        )
        user_input = gr.Textbox(
            label="Your Task or Response",
            placeholder="Enter your task here or provide assistance when asked.",
            lines=3,
            interactive=True,
            elem_id="user_input",
        )
        with gr.Row():
            stop_button = gr.Button(
                "⏹️ Stop", interactive=False, variant="stop", scale=2
            )
            pause_resume_button = gr.Button(
                "⏸️ Pause", interactive=False, variant="secondary", scale=2, visible=True
            )
            clear_button = gr.Button(
                "🗑️ Clear", interactive=True, variant="secondary", scale=2
            )
            run_button = gr.Button("▶️ Submit Task", variant="primary", scale=3)

        browser_view = gr.HTML(
            value="<div style='width:100%; height:50vh; display:flex; justify-content:center; align-items:center; border:1px solid #ccc; background-color:#f0f0f0;'><p>Browser View (Requires Headless=True)</p></div>",
            label="Browser Live View",
            elem_id="browser_view",
            visible=False,
        )
        with gr.Column():
            gr.Markdown("### Task Outputs")
            agent_history_file = gr.File(label="Agent History JSON", interactive=False)
            recording_gif = gr.Image(
                label="Task Recording GIF",
                format="gif",
                interactive=False,
                type="filepath",
            )

    # --- Store Components in Manager ---
    tab_components.update(
        dict(
            chatbot=chatbot,
            user_input=user_input,
            clear_button=clear_button,
            run_button=run_button,
            stop_button=stop_button,
            pause_resume_button=pause_resume_button,
            agent_history_file=agent_history_file,
            recording_gif=recording_gif,
            browser_view=browser_view,
        )
    )
    webui_manager.add_components(
        "browser_use_agent", tab_components
    )  # Use "browser_use_agent" as tab_name prefix

    all_managed_components = set(
        webui_manager.get_components()
    )  # Get all components known to manager
    run_tab_outputs = list(tab_components.values())

    async def submit_wrapper(
            components_dict: Dict[Component, Any],
    ) -> AsyncGenerator[Dict[Component, Any], None]:
        """Wrapper for handle_submit that yields its results."""
        async for update in handle_submit(webui_manager, components_dict):
            yield update

    async def stop_wrapper() -> AsyncGenerator[Dict[Component, Any], None]:
        """Wrapper for handle_stop."""
        update_dict = await handle_stop(webui_manager)
        yield update_dict

    async def pause_resume_wrapper() -> AsyncGenerator[Dict[Component, Any], None]:
        """Wrapper for handle_pause_resume."""
        update_dict = await handle_pause_resume(webui_manager)
        yield update_dict

    async def clear_wrapper() -> AsyncGenerator[Dict[Component, Any], None]:
        """Wrapper for handle_clear."""
        update_dict = await handle_clear(webui_manager)
        yield update_dict

    # --- Connect Event Handlers using the Wrappers --
    run_button.click(
        fn=submit_wrapper, inputs=all_managed_components, outputs=run_tab_outputs, trigger_mode="multiple"
    )
    user_input.submit(
        fn=submit_wrapper, inputs=all_managed_components, outputs=run_tab_outputs
    )
    stop_button.click(fn=stop_wrapper, inputs=None, outputs=run_tab_outputs)
    pause_resume_button.click(
        fn=pause_resume_wrapper, inputs=None, outputs=run_tab_outputs
    )
    clear_button.click(fn=clear_wrapper, inputs=None, outputs=run_tab_outputs)
