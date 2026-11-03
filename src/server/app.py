
import asyncio
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState


app = FastAPI(title="Browser Agent Backend")


# Temporary: one extension connection for local development
extension_socket: WebSocket | None = None


# Stores browser actions waiting for their responses
pending_actions: dict[str, asyncio.Future] = {}


# Prevent overlapping WebSocket sends
send_lock = asyncio.Lock()


# Browser actions currently supported by our extension
SUPPORTED_ACTIONS = {
    "NAVIGATE",
    "GET_STATE",
    "TYPE",
    "CLICK",
    "PRESS_KEY",
    "SCROLL",
    "GET_TABS",
    "SWITCH_TAB"
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
async def test_action(request: ActionRequest):

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

    await websocket.accept()

    extension_socket = websocket

    print("Chrome extension connected!")

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

                # Connection activity received
                pass

            else:

                print("Unknown extension message:", data)

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
