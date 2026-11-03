import asyncio
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from src.server.security import authenticate_websocket, verify_http_auth


app = FastAPI(title="Browser Agent Backend")


# Active extension connection for browser operations
extension_socket: WebSocket | None = None


# Stores browser actions waiting for their responses
pending_actions: dict[str, asyncio.Future] = {}


# Prevent overlapping WebSocket sends
send_lock = asyncio.Lock()


# Browser actions currently supported by our extension
SUPPORTED_ACTIONS = {
    "NAVIGATE",
    "GET_STATE",
    "GET_DOM_STATE",
    "TYPE",
    "CLICK",
    "PRESS_KEY",
    "SCROLL",
    "GET_TABS",
    "SWITCH_TAB",
    "CREATE_NEW_TAB",
    "CLOSE_TAB",
    "GO_BACK",
    "GET_PAGE_CONTENT",
    "EXTRACT_CONTENT",
}


ACTION_TIMEOUT = 30


# --------------------------------------------------
# Request model
# --------------------------------------------------

class ActionRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------
# Browser action dispatcher
# --------------------------------------------------

async def execute_browser_action(
    action: str,
    params: dict[str, Any]
):
    global extension_socket

    # Verify extension connection
    if (
        extension_socket is None
        or extension_socket.client_state != WebSocketState.CONNECTED
    ):
        raise HTTPException(
            status_code=503,
            detail="Chrome extension is not connected"
        )

    # Validate action
    if action not in SUPPORTED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action: {action}"
        )

    # Generate unique request ID
    request_id = str(uuid4())

    # Create a Future to wait for the browser response
    future = asyncio.get_running_loop().create_future()

    # Register pending request
    pending_actions[request_id] = future

    try:
        # Send command to Chrome extension
        async with send_lock:
            if (
                extension_socket is None
                or extension_socket.client_state != WebSocketState.CONNECTED
            ):
                raise HTTPException(
                    status_code=503,
                    detail="Chrome extension disconnected"
                )

            await extension_socket.send_json({
                "type": "EXECUTE_ACTION",
                "request_id": request_id,
                "action": action,
                "params": params
            })

        print(f"Action dispatched: {action} | ID: {request_id}")

        # Wait for the matching browser response
        result = await asyncio.wait_for(
            future,
            timeout=ACTION_TIMEOUT
        )

        print(f"Action completed: {action} | ID: {request_id}")

        return result

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Browser action timed out: {action}"
        )

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"Browser communication failed: {error}"
        )

    finally:
        # Always clean up the pending request
        pending_actions.pop(request_id, None)

        if not future.done():
            future.cancel()


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "extension_connected": (
            extension_socket is not None
            and extension_socket.client_state == WebSocketState.CONNECTED
        ),
        "pending_actions": len(pending_actions)
    }


# --------------------------------------------------
# Unified browser action endpoint
# --------------------------------------------------

@app.post("/test/action")
async def test_action(request: ActionRequest, token: str = Depends(verify_http_auth)):
    action = request.action.strip().upper()

    result = await execute_browser_action(
        action=action,
        params=request.params
    )

    return result


@app.post("/api/v1/action")
async def execute_action_rpc(request: ActionRequest, token: str = Depends(verify_http_auth)):
    """
    Production internal RPC endpoint for ExtensionBrowserContext and controllers.
    Dispatches browser actions to the Chrome Extension via WebSocket.
    Requires Bearer token authentication.
    """
    action = request.action.strip().upper()

    result = await execute_browser_action(
        action=action,
        params=request.params
    )

    return result


# --------------------------------------------------
# WebSocket connection with Chrome extension
# --------------------------------------------------

@app.websocket("/ws/extension")
async def extension_connection(websocket: WebSocket):
    global extension_socket

    # Authenticate token, host, and origin before accepting
    if not await authenticate_websocket(websocket):
        return

    # Prevent connection hijacking if an authenticated extension is already connected
    if extension_socket is not None and extension_socket.client_state == WebSocketState.CONNECTED:
        await websocket.close(code=1008, reason="Another extension connection is already active")
        return

    await websocket.accept()

    extension_socket = websocket

    print("Chrome extension authenticated and connected!")

    async with send_lock:
        await websocket.send_json({
            "type": "WELCOME",
            "message": "Hello Chrome"
        })

    try:
        while True:
            # Receive messages from Chrome
            data = await websocket.receive_json()

            message_type = data.get("type")

            # Handshake
            if message_type == "HELLO":
                print("Chrome handshake completed!")

                async with send_lock:
                    await websocket.send_json({
                        "type": "ACK",
                        "message": "Hello Python"
                    })

            # Browser action result
            elif message_type == "ACTION_RESULT":
                request_id = data.get("request_id")

                future = pending_actions.get(request_id)

                if future is not None and not future.done():
                    # Resolve the Future with the actual browser result
                    future.set_result(data)
                    print(f"Received action result: {request_id}")
                else:
                    print(f"Unmatched action result: {request_id}")

            # Heartbeat
            elif message_type == "HEARTBEAT":
                pass

            else:
                print("Unknown extension message type:", message_type)

    except WebSocketDisconnect:
        print("Chrome extension disconnected!")

    finally:
        # Only clear the connection if this is still the active socket
        if extension_socket is websocket:
            extension_socket = None

            # Fail any actions still waiting for responses
            for future in list(pending_actions.values()):
                if not future.done():
                    future.set_exception(
                        ConnectionError("Chrome extension disconnected")
                    )

            pending_actions.clear()
