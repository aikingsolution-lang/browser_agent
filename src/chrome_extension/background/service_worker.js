const BACKEND_URL = "ws://127.0.0.1:8765/ws/extension";

let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;


// Establish connection with Python backend
function connectToBackend() {

    if (
        socket &&
        (socket.readyState === WebSocket.OPEN ||
         socket.readyState === WebSocket.CONNECTING)
    ) {
        return;
    }

    console.log("Connecting to Browser Agent backend...");

    const currentSocket = new WebSocket(BACKEND_URL);
    socket = currentSocket;

    // Connection established
    currentSocket.addEventListener("open", () => {

        console.log("Connected to Python backend!");

        startHeartbeat(currentSocket);

    });


    // Receive messages from Python
    currentSocket.addEventListener("message", (event) => {

        let message;

        try {
            message = JSON.parse(event.data);
        } catch (error) {
            console.error("Invalid server message:", error);
            return;
        }

        console.log("Message from Python:", message);

        if (message.type === "WELCOME") {

            // Send handshake response
            currentSocket.send(JSON.stringify({
                type: "HELLO",
                client: "chrome-extension",
                version: "0.1.0"
            }));

        }

        if (message.type === "ACK") {

            console.log("WebSocket handshake completed!");

        }

    });


    // Handle errors
    currentSocket.addEventListener("error", (error) => {

        console.error("WebSocket error:", error);

    });


    // Handle disconnection
    currentSocket.addEventListener("close", () => {

        console.log("Disconnected from Python backend.");

        stopHeartbeat();

        if (socket === currentSocket) {
            socket = null;
        }

        scheduleReconnect();

    });

}


// Keep connection active
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


// Stop heartbeat
function stopHeartbeat() {

    if (heartbeatTimer !== null) {

        clearInterval(heartbeatTimer);
        heartbeatTimer = null;

    }

}


// Retry connection
function scheduleReconnect() {

    if (reconnectTimer !== null) {
        return;
    }

    reconnectTimer = setTimeout(() => {

        reconnectTimer = null;
        connectToBackend();

    }, 3000);

}


// Chrome extension lifecycle events
chrome.runtime.onInstalled.addListener(connectToBackend);

chrome.runtime.onStartup.addListener(connectToBackend);


// Initial connection
connectToBackend();