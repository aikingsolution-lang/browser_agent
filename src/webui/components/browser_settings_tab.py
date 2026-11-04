import os
from distutils.util import strtobool
import gradio as gr
import logging
from gradio.components import Component

from src.webui.webui_manager import WebuiManager
from src.utils import config

logger = logging.getLogger(__name__)

async def close_browser(webui_manager: WebuiManager):
    """
    Close browser
    """
    if webui_manager.bu_current_task and not webui_manager.bu_current_task.done():
        webui_manager.bu_current_task.cancel()
        webui_manager.bu_current_task = None

    if webui_manager.bu_browser_context:
        logger.info("⚠️ Closing browser context when changing browser config.")
        await webui_manager.bu_browser_context.close()
        webui_manager.bu_browser_context = None

    if webui_manager.bu_browser:
        logger.info("⚠️ Closing browser when changing browser config.")
        await webui_manager.bu_browser.close()
        webui_manager.bu_browser = None

def create_browser_settings_tab(webui_manager: WebuiManager):
    """
    Creates the browser settings tab configured for Chrome Extension architecture.
    """
    tab_components = {}

    with gr.Group():
        gr.Markdown(
            "### 🌐 Chrome Extension Architecture (Mandatory)\n"
            "The agent operates directly within your running Google Chrome browser via the authenticated AI Kings extension. "
            "Playwright browser launches are disabled."
        )

    with gr.Group():
        with gr.Row():
            keep_browser_open = gr.Checkbox(
                label="Keep Browser Tabs Open",
                value=bool(strtobool(os.getenv("KEEP_BROWSER_OPEN", "true"))),
                info="Keep agent-created tabs open between tasks",
                interactive=True,
            )
            backend_url = gr.Textbox(
                label="FastAPI Backend URL",
                value=os.getenv("EXTENSION_SERVER_URL", "http://127.0.0.1:8765"),
                info="WebSocket bridge endpoint for Chrome Extension",
                interactive=False,
            )

    with gr.Group():
        with gr.Row():
            window_w = gr.Number(
                label="Window Width",
                value=1280,
                info="Browser viewport reference width",
                interactive=True,
            )
            window_h = gr.Number(
                label="Window Height",
                value=1100,
                info="Browser viewport reference height",
                interactive=True,
            )

    with gr.Group():
        with gr.Row():
            save_agent_history_path = gr.Textbox(
                label="Agent History Save Path",
                value="./tmp/agent_history",
                info="Specify the directory where agent history should be saved.",
                interactive=True,
            )
            save_download_path = gr.Textbox(
                label="Save Directory for browser downloads",
                value="./tmp/downloads",
                info="Specify the directory where downloaded files should be saved.",
                interactive=True,
            )

    tab_components.update(
        dict(
            keep_browser_open=keep_browser_open,
            window_h=window_h,
            window_w=window_w,
            save_agent_history_path=save_agent_history_path,
            save_download_path=save_download_path,
        )
    )
    webui_manager.add_components("browser_settings", tab_components)

    async def close_wrapper():
        """Wrapper for close_browser."""
        await close_browser(webui_manager)

    keep_browser_open.change(close_wrapper)
