
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="Browser Agent Backend")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.websocket("/ws/extension")
async def extension_connection(websocket: WebSocket):

    await websocket.accept()

    print("Chrome extension connected!")

    # Send a welcome message to Chrome
    await websocket.send_json({
        "type": "WELCOME",
        "message": "Hello Chrome"
    })

    try:
        while True:

            # Receive message from extension
            data = await websocket.receive_json()

            print("Received:", data)

            if data.get("type") == "HELLO":

                await websocket.send_json({
                    "type": "ACK",
                    "message": "Hello Python"
                })

    except WebSocketDisconnect:
        print("Chrome extension disconnected!")
