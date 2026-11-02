
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


// Execute browser action
async function executeAction(message, currentSocket) {
    try {
        const { action, params = {} } = message;

        if (action === "NAVIGATE") {
            const url = new URL(params.url);

            // Only allow regular web URLs
            if (!["http:", "https:"].includes(url.protocol)) {
                throw new Error("Unsupported URL protocol");
            }

            // Get currently active Chrome tab
            const [activeTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true
            });

            // Navigate existing tab or create a new one
            if (activeTab?.id !== undefined) {
                await chrome.tabs.update(activeTab.id, {
                    url: url.href
                });
            } else {
                await chrome.tabs.create({
                    url: url.href
                });
            }

            sendResult(currentSocket, {
                type: "ACTION_RESULT",
                action: "NAVIGATE",
                success: true,
                result: `Navigated to ${url.href}`
            });

        } else {
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
