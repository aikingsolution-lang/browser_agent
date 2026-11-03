"""
DOM reconstruction helper for Browser Use 0.1.48 integration with Chrome Extension.

Reuses DomService._construct_dom_tree() from the installed browser-use package
to construct a valid BrowserState from Chrome Extension extraction data.
"""

from typing import Any
from browser_use.browser.views import BrowserState, TabInfo
from browser_use.dom.service import DomService
from browser_use.dom.views import DOMElementNode, SelectorMap


async def reconstruct_browser_state(raw_data: dict[str, Any]) -> BrowserState:
    """
    Reconstructs a real BrowserState from the raw result returned by GET_DOM_STATE.
    
    Expected raw_data shape:
    {
        "url": str,
        "title": str,
        "tab_id": int,
        "eval_page": {
            "rootId": int | str,
            "map": dict
        },
        "pixels_above": int,
        "pixels_below": int
    }
    """
    if not isinstance(raw_data, dict):
        raise TypeError(f"Expected dict for raw_data, got {type(raw_data).__name__}")

    eval_page = raw_data.get("eval_page")
    if not isinstance(eval_page, dict):
        raise ValueError("Invalid DOM extraction payload: missing 'eval_page' dictionary")

    if "map" not in eval_page or "rootId" not in eval_page:
        raise ValueError("Invalid eval_page: missing 'map' or 'rootId' keys")

    if not isinstance(eval_page["map"], dict):
        raise ValueError("Invalid eval_page['map']: expected dictionary")

    # Reuse DomService's _construct_dom_tree directly from installed browser_use package.
    # page=None is safe because _construct_dom_tree and _parse_node operate purely on eval_page dictionary.
    dom_service = DomService(page=None)
    element_tree, selector_map = await dom_service._construct_dom_tree(eval_page)

    if not isinstance(element_tree, DOMElementNode):
        raise ValueError(f"DOM reconstruction failed: root is {type(element_tree)}, expected DOMElementNode")

    url = raw_data.get("url", "")
    title = raw_data.get("title", "")
    tab_id = raw_data.get("tab_id", 0)
    pixels_above = int(raw_data.get("pixels_above", 0))
    pixels_below = int(raw_data.get("pixels_below", 0))

    # Use the actual Chrome tab ID in TabInfo (do not hardcode page_id=0)
    tab_info = TabInfo(page_id=tab_id, url=url, title=title)

    return BrowserState(
        element_tree=element_tree,
        selector_map=selector_map,
        url=url,
        title=title,
        tabs=[tab_info],
        screenshot=None,
        pixels_above=pixels_above,
        pixels_below=pixels_below,
    )
