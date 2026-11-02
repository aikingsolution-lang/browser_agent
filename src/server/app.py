
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, HttpUrl
from starlette.websockets import WebSocketState

app = FastAPI(title="Browser Agent Backend")

# Temporary: one extension connection for local development
extension_socket: WebSocket | None = None


class NavigateRequest(BaseModel):
    url: HttpUrl


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "extension_connected": (
            extension_socket is not None
            and extension_socket.client_state == WebSocketState.CONNECTED
        )
    }


@app.post("/test/navigate")
async def test_navigation(request: NavigateRequest):

    if (
        extension_socket is None
        or extension_socket.client_state != WebSocketState.CONNECTED
    ):
        return {"error": "Chrome extension is not connected"}

    await extension_socket.send_json({
        "type": "EXECUTE_ACTION",
        "action": "NAVIGATE",
        "params": {
            "url": str(request.url)
        }
    })

    return {"status": "command_sent", "url": str(request.url)}


@app.post("/test/state")
async def test_page_state():

    if (
        extension_socket is None
        or extension_socket.client_state != WebSocketState.CONNECTED
    ):
        return {"error": "Chrome extension is not connected"}

    await extension_socket.send_json({
        "type": "EXECUTE_ACTION",
        "action": "GET_STATE",
        "params": {}
    })

    return {"status": "state_request_sent"}

@app.websocket("/ws/extension")
async def extension_connection(websocket: WebSocket):
    global extension_socket

    await websocket.accept()
    extension_socket = websocket

    print("Chrome extension connected!")

    await websocket.send_json({
        "type": "WELCOME",
        "message": "Hello Chrome"
    })

    try:
        while True:
            data = await websocket.receive_json()
            print("Received:", data)

            if data.get("type") == "HELLO":
                await websocket.send_json({
                    "type": "ACK",
                    "message": "Hello Python"
                })

    except WebSocketDisconnect:
        print("Chrome extension disconnected!")

    finally:
        if extension_socket is websocket:
            extension_socket = None
