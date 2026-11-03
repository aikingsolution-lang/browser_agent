
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


// Execute browser action

async function executeAction(message, currentSocket) {
    try {
        const { action, params = {} } = message;

        // Read webpage information
        if (action === "GET_STATE") {
            const state = await getPageState();

            sendResult(currentSocket, {
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

            sendResult(currentSocket, {
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

            sendResult(currentSocket, {
                type: "ACTION_RESULT",
                action: "CLICK",
                success: true,
                result: result
            });
        }

        // Always open requested websites in a NEW TAB
        else if (action === "NAVIGATE") {
            const url = new URL(params.url);

            // Only allow HTTP and HTTPS
            if (!["http:", "https:"].includes(url.protocol)) {
                throw new Error("Unsupported URL protocol");
            }

            const newTab = await chrome.tabs.create({
                url: url.href,
                active: true
            });

            sendResult(currentSocket, {
                type: "ACTION_RESULT",
                action: "NAVIGATE",
                success: true,
                result: `Opened ${url.href} in a new tab`,
                tabId: newTab.id
            });
        }

        else {
            throw new Error(`Unsupported action: ${action}`);
        }

    } catch (error) {
        sendResult(currentSocket, {
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
