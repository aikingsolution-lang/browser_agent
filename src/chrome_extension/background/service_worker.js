
const BACKEND_URL = "ws://127.0.0.1:8765/ws/extension";

let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;


// Connect to Python backend
function connectToBackend() {
    if (
        socket &&
        (socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING)
    ) {
        return;
    }

    console.log("Connecting to backend...");

    const currentSocket = new WebSocket(BACKEND_URL);
    socket = currentSocket;

    currentSocket.addEventListener("open", () => {
        console.log("Connected to Python backend!");
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


async function getPageState() {

    // Find the active Chrome tab
    const [activeTab] = await chrome.tabs.query({
        active: true,
        lastFocusedWindow: true
    });

    if (!activeTab || activeTab.id === undefined) {
        throw new Error("No active browser tab found");
    }

    // Chrome internal pages cannot be inspected this way
    if (!/^https?:\/\//i.test(activeTab.url || "")) {
        throw new Error("This page cannot be inspected");
    }

    // Execute JavaScript inside the webpage
    const results = await chrome.scripting.executeScript({
        target: {
            tabId: activeTab.id
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

async function typeIntoElement(tabId, xpath, text) {

    const results = await chrome.scripting.executeScript({
        target: { tabId },

        args: [xpath, text],

        func: (xpath, text) => {

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

    return result;
}


async function pressKeyOnElement(tabId, xpath, key) {

    if (key.toUpperCase() !== "ENTER") {
        throw new Error("Currently only ENTER is supported");
    }

    const results = await chrome.scripting.executeScript({
        target: { tabId },
        args: [xpath],

        func: (xpath) => {

            // Find the element
            const element = document.evaluate(
                xpath,
                document,
                null,
                XPathResult.FIRST_ORDERED_NODE_TYPE,
                null
            ).singleNodeValue;

            if (!(element instanceof HTMLInputElement)) {
                return {
                    success: false,
                    error: "Target must be an input element"
                };
            }

            if (
                element.disabled ||
                element.readOnly ||
                ["password", "file", "hidden"].includes(element.type)
            ) {
                return {
                    success: false,
                    error: "Input cannot be submitted"
                };
            }

            const form = element.form;

            if (!form) {
                return {
                    success: false,
                    error: "Input has no associated form"
                };
            }

            // Focus input
            element.focus();

            // Submit using browser's native form behavior
            form.requestSubmit();

            return {
                success: true,
                key: "ENTER",
                method: "requestSubmit"
            };
        }
    });

    const result = results?.[0]?.result;

    if (!result?.success) {
        throw new Error(result?.error || "Enter action failed");
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
            const state = await getPageState();

            sendActionResult({
                type: "ACTION_RESULT",
                action: "GET_STATE",
                success: true,
                result: state
            });
        }
        else if (action === "TYPE") {

            const [activeTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true
            });

            if (!activeTab || activeTab.id === undefined) {
                throw new Error("No active tab found");
            }

            const result = await typeIntoElement(
                activeTab.id,
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

            const [activeTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true
            });

            if (!activeTab || activeTab.id === undefined) {
                throw new Error("No active tab found");
            }

            const result = await clickElement(
                activeTab.id,
                params.xpath
            );

            sendActionResult({
                type: "ACTION_RESULT",
                action: "CLICK",
                success: true,
                result: result
            });
        }

        // Always open requested websites in a NEW TAB
      
else if (action === "NAVIGATE") {

    const url = new URL(params.url);

    if (!["http:", "https:"].includes(url.protocol)) {
        throw new Error("Unsupported URL protocol");
    }

    // Create a new tab, preserving existing tabs
    const newTab = await chrome.tabs.create({
        url: url.href,
        active: true
    });

    if (newTab.id === undefined) {
        throw new Error("Could not create browser tab");
    }

    console.log("New tab created:", newTab.id);

    // Wait until webpage finishes loading
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
}



        else if (action === "PRESS_KEY") {

            const [activeTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true
            });

            if (!activeTab || activeTab.id === undefined) {
                throw new Error("No active tab found");
            }

            const result = await pressKeyOnElement(
                activeTab.id,
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

            const [activeTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true
            });

            if (!activeTab || activeTab.id === undefined) {
                throw new Error("No active tab found");
            }

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
                activeTab.id,
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

            const tabList = tabs
                .filter(tab =>
                    tab.id !== undefined &&
                    /^https?:\/\//i.test(tab.url || "")
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
