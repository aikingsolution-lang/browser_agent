// Load Browser Use buildDomTree.js for Pattern A execution in service worker
try {
    importScripts("../injected/buildDomTree.js");
} catch (importErr) {
    console.warn("importScripts for buildDomTree failed (will use Pattern B):", importErr);
}

// Session authentication token (DEF-05)
try {
    importScripts("auth_token.js");
} catch (_) {}
const AUTH_TOKEN = (typeof self !== "undefined" && self.AUTH_TOKEN) || "";

// In-memory fallback and persistent session storage for agent-owned tabs (DEF-04)
let agentTabIdsInMemory = new Set();

async function getStoredAgentTabIds() {
    if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.session) {
        try {
            const data = await chrome.storage.session.get("agentTabIds");
            if (Array.isArray(data.agentTabIds)) {
                return new Set(data.agentTabIds);
            }
        } catch (_) {}
    }
    return agentTabIdsInMemory;
}

async function addAgentTabId(tabId) {
    if (!Number.isInteger(tabId)) return;
    const ids = await getStoredAgentTabIds();
    ids.add(tabId);
    agentTabIdsInMemory = ids;
    if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.session) {
        try {
            await chrome.storage.session.set({ agentTabIds: Array.from(ids) });
        } catch (_) {}
    }
}

async function removeAgentTabId(tabId) {
    const ids = await getStoredAgentTabIds();
    ids.delete(tabId);
    agentTabIdsInMemory = ids;
    if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.session) {
        try {
            await chrome.storage.session.set({ agentTabIds: Array.from(ids) });
        } catch (_) {}
    }
}

if (typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.onRemoved) {
    chrome.tabs.onRemoved.addListener(async (closedTabId) => {
        await removeAgentTabId(closedTabId);
    });
}

const BACKEND_URL = "ws://127.0.0.1:8765/ws/extension";

let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;


// Connect to Python backend without credentials in URL (Stage 1)
function connectToBackend() {
    if (
        socket &&
        (socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING)
    ) {
        return;
    }

    console.log("Connecting to backend...");

    // Connect with zero credentials in URL
    const currentSocket = new WebSocket(BACKEND_URL);
    socket = currentSocket;

    currentSocket.addEventListener("open", () => {
        console.log("Connected to Python backend! Transmitting AUTH handshake...");
        // Transmit session token via in-message AUTH handshake
        currentSocket.send(JSON.stringify({
            type: "AUTH",
            token: AUTH_TOKEN
        }));
        startHeartbeat(currentSocket);
    });

    // Receive commands from Python
    currentSocket.addEventListener("message", async (event) => {
        let message;

        try {
            message = JSON.parse(event.data);
        } catch (error) {
            console.error("Invalid server message:", error);
            return;
        }

        console.log("Message from Python:", message);

        if (message.type === "WELCOME") {
            currentSocket.send(JSON.stringify({
                type: "HELLO",
                client: "chrome-extension",
                version: "0.1.0"
            }));
        }

        if (message.type === "ACK") {
            console.log("WebSocket handshake completed!");
        }

        // Handle browser actions
        if (message.type === "EXECUTE_ACTION") {
            await executeAction(message, currentSocket);
        }
    });

    currentSocket.addEventListener("error", (error) => {
        console.error("WebSocket error:", error);
    });

    currentSocket.addEventListener("close", () => {
        console.log("Backend disconnected!");

        stopHeartbeat();

        if (socket === currentSocket) {
            socket = null;
        }

        scheduleReconnect();
    });
}


async function resolveTargetTabId(requestedTabId) {
    if (requestedTabId === undefined || requestedTabId === null) {
        throw new Error("Target tabId is required for agent-controlled browser actions");
    }

    const tid = Number(requestedTabId);
    if (!Number.isInteger(tid) || tid < 0) {
        throw new Error(`Invalid target tabId: ${requestedTabId}`);
    }

    try {
        const tab = await chrome.tabs.get(tid);
        if (tab && tab.id !== undefined) {
            return tab.id;
        }
    } catch (err) {
        throw new Error(`Target tab ${tid} is closed or invalid: ${err.message}`);
    }

    throw new Error(`Target tab ${tid} not found`);
}


async function getPageState(targetTabIdParam) {

    // Resolve explicit target tab ID without active tab fallback
    const targetTabId = await resolveTargetTabId(targetTabIdParam);
    const targetTab = await chrome.tabs.get(targetTabId);

    // Chrome internal pages cannot be inspected this way
    if (!/^https?:\/\//i.test(targetTab.url || "")) {
        throw new Error("This page cannot be inspected: " + (targetTab.url || "unsupported protocol"));
    }

    // Execute JavaScript inside the webpage
    const results = await chrome.scripting.executeScript({
        target: {
            tabId: targetTabId
        },

        func: () => {

            // Check whether an element is visible
            function isVisible(element) {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();

                return (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== "none" &&
                    style.visibility !== "hidden"
                );
            }

            // Generate an XPath for an element
            function getXPath(element) {
                const path = [];

                while (
                    element &&
                    element.nodeType === Node.ELEMENT_NODE
                ) {
                    const tag = element.localName;
                    let index = 1;

                    let sibling = element.previousElementSibling;

                    while (sibling) {
                        if (sibling.localName === tag) {
                            index++;
                        }
                        sibling = sibling.previousElementSibling;
                    }

                    path.unshift(`${tag}[${index}]`);

                    if (tag === "html") break;

                    element = element.parentElement;
                }

                return "/" + path.join("/");
            }

            // Find interactive webpage elements
            const selectors = [
                "a[href]",
                "button",
                "input",
                "textarea",
                "select",
                '[role="button"]',
                '[role="link"]',
                '[contenteditable="true"]',
                '[tabindex]:not([tabindex="-1"])'
            ].join(",");

            const elements = Array.from(
                document.querySelectorAll(selectors)
            )
                .filter(isVisible)
                .slice(0, 80);

            const interactiveElements = elements.map(
                (element, index) => {

                    const text = (
                        element.innerText ||
                        element.textContent ||
                        element.getAttribute("aria-label") ||
                        element.getAttribute("placeholder") ||
                        ""
                    ).trim().replace(/\s+/g, " ");

                    return {
                        index,
                        tag: element.localName,
                        text: text.slice(0, 200),
                        xpath: getXPath(element),
                        placeholder: element.getAttribute("placeholder"),
                        aria_label: element.getAttribute("aria-label"),
                        type: element.getAttribute("type"),
                        href: element.href || null,
                        disabled: Boolean(element.disabled)
                    };
                }
            );

            return {
                url: location.href,
                title: document.title,

                // Limit webpage text to avoid huge responses
                page_text: (
                    document.body?.innerText || ""
                ).slice(0, 4000),

                interactive_elements: interactiveElements
            };
        }
    });

    return results[0].result;
}


async function getDOMState(targetTabIdParam) {
    const tabId = await resolveTargetTabId(targetTabIdParam);
    let targetTab = await chrome.tabs.get(tabId);

    // Fix tab loading race: wait for tab to finish loading if in progress
    if (targetTab.status === "loading") {
        const loadedTab = await waitForTabLoad(tabId);
        if (loadedTab) {
            targetTab = loadedTab;
        }
    }

    // Support empty page extraction for initial about:blank bootstrap (Stage 3)
    if (targetTab.url === "about:blank") {
        const ownedIds = await getStoredAgentTabIds();
        const allTabs = (await chrome.tabs.query({}))
            .filter(t => t.id !== undefined && (t.url === "about:blank" || /^https?:\/\//i.test(t.url || "")) && ownedIds.has(t.id))
            .map(t => ({
                tabId: t.id,
                url: t.url || "",
                title: t.title || "",
                active: t.active
            }));

        return {
            url: "about:blank",
            title: "",
            tab_id: tabId,
            eval_page: {
                rootId: "0",
                map: {
                    "0": {
                        tagName: "body",
                        xpath: "/html/body",
                        attributes: {},
                        children: [],
                        isVisible: true
                    }
                }
            },
            pixels_above: 0,
            pixels_below: 0,
            all_tabs: allTabs
        };
    }

    if (!/^https?:\/\//i.test(targetTab.url || "")) {
        throw new Error("This page cannot be inspected: " + (targetTab.url || "unsupported protocol"));
    }

    const defaultArgs = {
        doHighlightElements: false,
        focusHighlightIndex: -1,
        viewportExpansion: 0,
        debugMode: false
    };

    async function runBuildDomTree() {
        let evalPage = null;

        // Pattern A: pass self-contained func to chrome.scripting.executeScript
        try {
            if (typeof buildDomTree === "function") {
                const results = await chrome.scripting.executeScript({
                    target: { tabId },
                    func: buildDomTree,
                    args: [defaultArgs]
                });
                if (results && results[0] && results[0].result) {
                    evalPage = results[0].result;
                }
            }
        } catch (patternAError) {
            if (patternAError?.message?.includes("Execution context was destroyed")) {
                throw patternAError;
            }
            console.warn("Pattern A execution failed, falling back to Pattern B:", patternAError);
        }

        // Pattern B Fallback: Packaged script execution
        if (!evalPage) {
            await chrome.scripting.executeScript({
                target: { tabId },
                files: ["injected/buildDomTree.js"]
            });

            const results = await chrome.scripting.executeScript({
                target: { tabId },
                func: (args) => {
                    const fn = window.buildDomTree || globalThis.buildDomTree;
                    if (typeof fn === "function") {
                        return fn(args);
                    }
                    throw new Error("window.buildDomTree is not defined on target page");
                },
                args: [defaultArgs]
            });

            if (results && results[0] && results[0].result) {
                evalPage = results[0].result;
            }
        }
        return evalPage;
    }

    let evalPage = null;
    try {
        evalPage = await runBuildDomTree();
    } catch (err) {
        if (err?.message?.includes("Execution context was destroyed")) {
            console.warn("Execution context was destroyed during DOM extraction, waiting for tab to load and retrying once:", err);
            const loadedTab = await waitForTabLoad(tabId, 15000);
            if (loadedTab) {
                targetTab = loadedTab;
            }
            evalPage = await runBuildDomTree();
        } else {
            throw err;
        }
    }

    if (!evalPage || !evalPage.map || evalPage.rootId === undefined) {
        throw new Error("Failed to extract DOM tree: invalid eval_page data returned");
    }

    // Measure scroll positions
    const scrollResults = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
            const scrollY = window.scrollY || window.pageYOffset || 0;
            const viewportHeight = window.innerHeight || 0;
            const docElement = document.documentElement;
            const totalHeight = docElement ? docElement.scrollHeight : (document.body ? document.body.scrollHeight : 0);
            return {
                pixels_above: Math.max(0, Math.round(scrollY)),
                pixels_below: Math.max(0, Math.round(totalHeight - (scrollY + viewportHeight)))
            };
        }
    });

    const scroll = scrollResults?.[0]?.result || { pixels_above: 0, pixels_below: 0 };

    const ownedIds = await getStoredAgentTabIds();

    // Strict tab filtering: empty ownership set authorizes ZERO tabs (DEF-04)
    const allTabs = (await chrome.tabs.query({}))
        .filter(t => t.id !== undefined && /^https?:\/\//i.test(t.url || "") && ownedIds.has(t.id))
        .map(t => ({
            tabId: t.id,
            url: t.url || "",
            title: t.title || "",
            active: t.active
        }));

    return {
        url: targetTab.url || "",
        title: targetTab.title || "",
        tab_id: tabId,
        eval_page: evalPage,
        pixels_above: scroll.pixels_above,
        pixels_below: scroll.pixels_below,
        all_tabs: allTabs
    };
}


async function typeIntoElement(tabId, xpath, text) {

    const results = await chrome.scripting.executeScript({
        target: { tabId },

        args: [xpath, text],

        func: (xpath, text) => {

            // Find element using XPath (supports relative and absolute paths)
            let element = document.evaluate(
                xpath,
                document,
                null,
                XPathResult.FIRST_ORDERED_NODE_TYPE,
                null
            ).singleNodeValue;

            if (!element && !xpath.startsWith("/")) {
                element = document.evaluate(
                    "/" + xpath,
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;
            }

            if (!(element instanceof HTMLElement)) {
                return {
                    success: false,
                    error: "Element not found"
                };
            }

            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);

            if (
                !rect.width ||
                !rect.height ||
                style.display === "none" ||
                style.visibility === "hidden"
            ) {
                return {
                    success: false,
                    error: "Element is not visible"
                };
            }

            // Handle input and textarea
            if (
                element instanceof HTMLInputElement ||
                element instanceof HTMLTextAreaElement
            ) {

                if (element.disabled || element.readOnly) {
                    return {
                        success: false,
                        error: "Element is not editable"
                    };
                }

                // Never type into password or file inputs
                if (
                    element instanceof HTMLInputElement &&
                    ["password", "file", "hidden"].includes(element.type)
                ) {
                    return {
                        success: false,
                        error: "Unsupported input type"
                    };
                }

                element.focus();

                // Use the native value setter.
                // This helps controlled frameworks detect changes.
                const prototype =
                    element instanceof HTMLInputElement
                        ? HTMLInputElement.prototype
                        : HTMLTextAreaElement.prototype;

                const setter = Object.getOwnPropertyDescriptor(
                    prototype,
                    "value"
                )?.set;

                if (!setter) {
                    return {
                        success: false,
                        error: "Could not update input value"
                    };
                }

                setter.call(element, text);

                element.dispatchEvent(
                    new InputEvent("input", {
                        bubbles: true,
                        inputType: "insertText",
                        data: text
                    })
                );

                element.dispatchEvent(
                    new Event("change", { bubbles: true })
                );

                return {
                    success: true,
                    tag: element.tagName,
                    placeholder: element.getAttribute("placeholder")
                };
            }

            // Handle contenteditable elements
            if (element.isContentEditable) {

                element.focus();
                element.textContent = text;

                element.dispatchEvent(
                    new InputEvent("input", {
                        bubbles: true,
                        inputType: "insertText",
                        data: text
                    })
                );

                return {
                    success: true,
                    tag: element.tagName
                };
            }

            return {
                success: false,
                error: "Element is not a supported text field"
            };
        }
    });

    const result = results?.[0]?.result;

    if (!result?.success) {
        throw new Error(result?.error || "Typing failed");
    }

    return result;
}


async function clickElement(tabId, xpath) {
    let navigationStarted = false;
    const onUpdated = (updatedTabId, changeInfo) => {
        if (updatedTabId === tabId && (changeInfo.status === "loading" || changeInfo.url)) {
            navigationStarted = true;
        }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId },

            args: [xpath],

            func: (xpath) => {

                // Find element using XPath
                const element = document.evaluate(
                    xpath,
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;

                if (!(element instanceof HTMLElement)) {
                    return {
                        success: false,
                        error: "Element not found"
                    };
                }

                // Check visibility
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);

                if (
                    rect.width === 0 ||
                    rect.height === 0 ||
                    style.display === "none" ||
                    style.visibility === "hidden"
                ) {
                    return {
                        success: false,
                        error: "Element is not visible"
                    };
                }

                // Check disabled state
                if (
                    element.matches(":disabled") ||
                    element.getAttribute("aria-disabled") === "true"
                ) {
                    return {
                        success: false,
                        error: "Element is disabled"
                    };
                }

                // Focus and click
                element.focus();
                element.click();

                return {
                    success: true,
                    tag: element.tagName,
                    text: element.innerText || element.textContent || ""
                };
            }
        });

        const result = results?.[0]?.result;

        if (!result?.success) {
            throw new Error(result?.error || "Click failed");
        }

        // Bounded check for potential navigation triggered by click (DEF-06, Stage 4)
        // Grace wait of 450ms supports ordinary anchor navigation, immediate JS navigation,
        // and delayed JS navigation up to 400ms (e.g. setTimeout(..., 400)).
        // Timing limitation: delayed navigation exceeding the grace window (e.g. >450ms)
        // cannot be detected synchronously without Chrome DevTools Protocol or webNavigation events.
        try {
            const currentTab = await chrome.tabs.get(tabId);
            if (currentTab.status === "loading") {
                navigationStarted = true;
            } else {
                await new Promise(r => setTimeout(r, 450));
                const postWaitTab = await chrome.tabs.get(tabId);
                if (postWaitTab.status === "loading") {
                    navigationStarted = true;
                }
            }
        } catch (_) {}

        if (navigationStarted) {
            await waitForTabLoad(tabId, 15000);
        }

        return result;
    } finally {
        chrome.tabs.onUpdated.removeListener(onUpdated);
    }
}


async function pressKeyOnElement(tabId, xpath, key) {

    const results = await chrome.scripting.executeScript({
        target: { tabId },
        args: [xpath || "", key || ""],

        func: (xpath, key) => {

            let element = null;
            if (xpath) {
                element = document.evaluate(
                    xpath,
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;

                if (!element && !xpath.startsWith("/")) {
                    element = document.evaluate(
                        "/" + xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                }
            }

            if (!element) {
                element = document.activeElement || document.body;
            }

            if (element && typeof element.focus === "function") {
                element.focus();
            }

            const isEnter = key && key.toUpperCase() === "ENTER";
            const keyCode = isEnter ? 13 : (key && key.toUpperCase() === "TAB" ? 9 : (key && key.toUpperCase() === "ESCAPE" ? 27 : 0));
            const keyEventInit = {
                key: key,
                code: isEnter ? "Enter" : key,
                keyCode: keyCode,
                which: keyCode,
                charCode: keyCode,
                bubbles: true,
                cancelable: true,
                composed: true
            };

            element.dispatchEvent(new KeyboardEvent("keydown", keyEventInit));
            element.dispatchEvent(new KeyboardEvent("keypress", keyEventInit));
            element.dispatchEvent(new KeyboardEvent("keyup", keyEventInit));

            // If Enter, trigger form.requestSubmit() via element.form or closest form ancestor (DEF-07)
            if (isEnter && element instanceof HTMLElement) {
                const form = element.form || (typeof element.closest === "function" ? element.closest("form") : null);
                if (form && typeof form.requestSubmit === "function") {
                    try {
                        form.requestSubmit();
                    } catch (_) {}
                }
            }

            return {
                success: true,
                key: key,
                targetTag: element ? element.tagName : "NONE"
            };
        }
    });

    const result = results?.[0]?.result;

    if (!result?.success) {
        throw new Error(result?.error || "Key action failed");
    }

    // If Enter was pressed, check for navigation load (DEF-06/DEF-07)
    if (key && key.toUpperCase() === "ENTER") {
        try {
            await new Promise(r => setTimeout(r, 200));
            const tab = await chrome.tabs.get(tabId);
            if (tab && tab.status === "loading") {
                await waitForTabLoad(tabId, 15000);
            }
        } catch (_) {}
    }

    return result;
}



async function scrollPage(tabId, direction, amount = 600) {

    const results = await chrome.scripting.executeScript({
        target: { tabId },
        args: [direction, amount],

        func: (direction, amount) => {

            const delta = direction === "DOWN"
                ? amount
                : -amount;

            window.scrollBy({
                top: delta,
                left: 0,
                behavior: "auto"
            });

            return {
                success: true,
                direction,
                scrollY: window.scrollY,
                pageHeight: document.documentElement.scrollHeight,
                viewportHeight: window.innerHeight
            };
        }
    });

    const result = results?.[0]?.result;

    if (!result?.success) {
        throw new Error("Scrolling failed");
    }

    return result;
}


function waitForTabLoad(tabId, timeoutMs = 30000) {

    return new Promise((resolve) => {

        let settled = false;
        let timeoutId;

        function finish(tab = null) {

            if (settled) return;

            settled = true;

            clearTimeout(timeoutId);

            chrome.tabs.onUpdated.removeListener(onUpdated);

            resolve(tab);
        }

        // Listen for tab loading completion
        function onUpdated(updatedTabId, changeInfo, tab) {

            if (
                updatedTabId === tabId &&
                changeInfo.status === "complete"
            ) {
                finish(tab);
            }
        }

        chrome.tabs.onUpdated.addListener(onUpdated);

        // Prevent waiting forever
        timeoutId = setTimeout(() => {
            finish(null);
        }, timeoutMs);

        // Check whether the page already finished loading
        chrome.tabs.get(tabId)
            .then(tab => {
                if (tab.status === "complete") {
                    finish(tab);
                }
            })
            .catch(() => finish(null));

    });
}


// Execute browser action

async function executeAction(message, currentSocket) {

        // Send action response with its original request ID
    const sendActionResult = (payload) => {

        sendResult(currentSocket, {
            ...payload,
            request_id: message.request_id ?? null
        });

    };
    try {
        const { action, params = {} } = message;

        // Read webpage information
        if (action === "GET_STATE") {
            const state = await getPageState(params.tabId);

            sendActionResult({
                type: "ACTION_RESULT",
                action: "GET_STATE",
                success: true,
                result: state
            });
        }
        else if (action === "GET_DOM_STATE") {
            const domState = await getDOMState(params.tabId);

            sendActionResult({
                type: "ACTION_RESULT",
                action: "GET_DOM_STATE",
                success: true,
                result: domState
            });
        }
        else if (action === "TYPE") {

            const targetTabId = await resolveTargetTabId(params.tabId);

            const result = await typeIntoElement(
                targetTabId,
                params.xpath,
                params.text
            );

            sendActionResult({
                type: "ACTION_RESULT",
                action: "TYPE",
                success: true,
                result: result
            });
        }

        else if (action === "CLICK") {

            const targetTabId = await resolveTargetTabId(params.tabId);

            const result = await clickElement(
                targetTabId,
                params.xpath
            );

            sendActionResult({
                type: "ACTION_RESULT",
                action: "CLICK",
                success: true,
                result: result
            });
        }

        else if (action === "NAVIGATE") {

            const url = new URL(params.url);

            if (!["http:", "https:"].includes(url.protocol)) {
                throw new Error("Unsupported URL protocol");
            }

            if (params.newTab) {
                const newTab = await chrome.tabs.create({
                    url: url.href,
                    active: true
                });

                if (newTab.id === undefined) {
                    throw new Error("Could not create browser tab");
                }
                await addAgentTabId(newTab.id);

                const loadedTab = await waitForTabLoad(newTab.id);

                sendActionResult({
                    type: "ACTION_RESULT",
                    action: "NAVIGATE",
                    success: true,
                    result: {
                        tabId: newTab.id,
                        url: loadedTab?.url || url.href,
                        title: loadedTab?.title || "",
                        loaded: Boolean(loadedTab)
                    }
                });
            } else {
                const targetTabId = await resolveTargetTabId(params.tabId);
                await chrome.tabs.update(targetTabId, { url: url.href });
                const loadedTab = await waitForTabLoad(targetTabId);

                sendActionResult({
                    type: "ACTION_RESULT",
                    action: "NAVIGATE",
                    success: true,
                    result: {
                        tabId: targetTabId,
                        url: loadedTab?.url || url.href,
                        title: loadedTab?.title || "",
                        loaded: Boolean(loadedTab)
                    }
                });
            }
        }

        else if (action === "CREATE_NEW_TAB") {
            const url = params.url || "about:blank";
            const active = params.active !== undefined ? Boolean(params.active) : false;
            const newTab = await chrome.tabs.create({ url, active });
            if (newTab.id === undefined) {
                throw new Error("Could not create browser tab");
            }
            await addAgentTabId(newTab.id); // Persist ownership in session storage (DEF-04)

            let loadedTab = null;
            if (url !== "about:blank") {
                loadedTab = await waitForTabLoad(newTab.id);
            }
            sendActionResult({
                type: "ACTION_RESULT",
                action: "CREATE_NEW_TAB",
                success: true,
                result: {
                    tabId: newTab.id,
                    url: loadedTab?.url || newTab.url || url,
                    title: loadedTab?.title || ""
                }
            });
        }

        else if (action === "CLOSE_TAB") {
            const targetTabId = await resolveTargetTabId(params.tabId);
            await chrome.tabs.remove(targetTabId);
            await removeAgentTabId(targetTabId); // Remove from persistent session storage (DEF-04)

            sendActionResult({
                type: "ACTION_RESULT",
                action: "CLOSE_TAB",
                success: true,
                result: {
                    tabId: targetTabId,
                    closed: true
                }
            });
        }

        else if (action === "GO_BACK") {
            const targetTabId = await resolveTargetTabId(params.tabId);
            await chrome.scripting.executeScript({
                target: { tabId: targetTabId },
                func: () => window.history.back()
            });
            await waitForTabLoad(targetTabId);
            sendActionResult({
                type: "ACTION_RESULT",
                action: "GO_BACK",
                success: true,
                result: {
                    tabId: targetTabId
                }
            });
        }

        else if (action === "PRESS_KEY") {

            const targetTabId = await resolveTargetTabId(params.tabId);

            const result = await pressKeyOnElement(
                targetTabId,
                params.xpath,
                params.key
            );

            sendActionResult({
                type: "ACTION_RESULT",
                action: "PRESS_KEY",
                success: true,
                result: result
            });
        }

        else if (action === "SCROLL") {

            const targetTabId = await resolveTargetTabId(params.tabId);

            const direction = params.direction || "DOWN";
            const amount = params.amount || 600;

            if (!["UP", "DOWN"].includes(direction)) {
                throw new Error("Invalid scroll direction");
            }

            if (
                !Number.isInteger(amount) ||
                amount < 1 ||
                amount > 3000
            ) {
                throw new Error("Invalid scroll amount");
            }

            const result = await scrollPage(
                targetTabId,
                direction,
                amount
            );

            sendActionResult({
                type: "ACTION_RESULT",
                action: "SCROLL",
                success: true,
                result
            });
        }

        else if (action === "GET_TABS") {

            const tabs = await chrome.tabs.query({});
            const ownedIds = await getStoredAgentTabIds();

            // Filter to agent-owned tabs only - empty set authorizes zero tabs (DEF-04)
            const tabList = tabs
                .filter(tab =>
                    tab.id !== undefined &&
                    /^https?:\/\//i.test(tab.url || "") &&
                    ownedIds.has(tab.id)
                )
                .map(tab => ({
                    tabId: tab.id,
                    title: tab.title || "",
                    url: tab.url || "",
                    active: tab.active,
                    windowId: tab.windowId
                }));

            sendActionResult({
                type: "ACTION_RESULT",
                action: "GET_TABS",
                success: true,
                result: tabList
            });
        }

        else if (action === "GET_PAGE_CONTENT") {
            const targetTabId = await resolveTargetTabId(params.tabId);
            const results = await chrome.scripting.executeScript({
                target: { tabId: targetTabId },
                args: [Boolean(params.stripLinkUrls)],
                func: (stripLinkUrls) => {
                    const body = document.body;
                    if (!body) return { content: "" };
                    const clone = body.cloneNode(true);
                    const toRemove = clone.querySelectorAll("script, style, noscript, svg");
                    toRemove.forEach(el => el.remove());
                    if (stripLinkUrls) {
                        const links = clone.querySelectorAll("a, img");
                        links.forEach(el => {
                            el.removeAttribute("href");
                            el.removeAttribute("src");
                        });
                    }
                    return {
                        content: (clone.innerText || clone.textContent || "").trim()
                    };
                }
            });

            const result = results?.[0]?.result || { content: "" };
            sendActionResult({
                type: "ACTION_RESULT",
                action: "GET_PAGE_CONTENT",
                success: true,
                result: result
            });
        }

        
else if (action === "SWITCH_TAB") {

    const tabId = Number(params.tabId);

    if (!Number.isInteger(tabId) || tabId < 0) {
        throw new Error("Invalid tab ID");
    }

    // Get the requested tab
    const tab = await chrome.tabs.get(tabId);

    // Bring its browser window into focus
    await chrome.windows.update(tab.windowId, {
        focused: true
    });

    // Activate the requested tab
    const updatedTab = await chrome.tabs.update(tabId, {
        active: true
    });

    sendActionResult({
        type: "ACTION_RESULT",
        action: "SWITCH_TAB",
        success: true,
        result: {
            tabId: updatedTab.id,
            title: updatedTab.title,
            url: updatedTab.url
        }
    });
}




        else {
            throw new Error(`Unsupported action: ${action}`);
        }

    } catch (error) {
        sendActionResult({
            type: "ACTION_RESULT",
            action: message.action,
            success: false,
            error: error.message
        });
    }
}



// Send action result to Python
function sendResult(currentSocket, result) {
    if (currentSocket.readyState === WebSocket.OPEN) {
        currentSocket.send(JSON.stringify(result));
    }
}


// Heartbeat
function startHeartbeat(currentSocket) {
    stopHeartbeat();

    heartbeatTimer = setInterval(() => {
        if (currentSocket.readyState === WebSocket.OPEN) {
            currentSocket.send(JSON.stringify({
                type: "HEARTBEAT",
                timestamp: Date.now()
            }));
        }
    }, 20000);
}


function stopHeartbeat() {
    if (heartbeatTimer !== null) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
}


// Reconnection
function scheduleReconnect() {
    if (reconnectTimer !== null) return;

    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectToBackend();
    }, 3000);
}


// Extension lifecycle
chrome.runtime.onInstalled.addListener(connectToBackend);
chrome.runtime.onStartup.addListener(connectToBackend);

connectToBackend();
