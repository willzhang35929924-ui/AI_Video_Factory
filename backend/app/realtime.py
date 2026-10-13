import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from app.schemas import ProductionEvent


class ProductionHub:
    def __init__(self):
        self.clients: dict[WebSocket, str | None] = {}

    async def connect(self, socket: WebSocket, task_id: str | None = None) -> None:
        await socket.accept()
        self.clients[socket] = task_id

    async def broadcast(self, event: ProductionEvent) -> None:
        for socket, task_id in list(self.clients.items()):
            if task_id and task_id != event.task_id:
                continue
            try:
                await socket.send_json(event.model_dump(mode="json"))
            except (WebSocketDisconnect, RuntimeError):
                self.clients.pop(socket, None)

    async def listen(self, socket: WebSocket, task_id: str | None = None) -> None:
        await self.connect(socket, task_id)
        await socket.send_json(ProductionEvent(type="snapshot", task_id=task_id, message="生产状态连接已建立").model_dump(mode="json"))
        try:
            while True:
                try:
                    message = await asyncio.wait_for(socket.receive(), timeout=20)
                    if message["type"] == "websocket.disconnect":
                        break
                except asyncio.TimeoutError:
                    await socket.send_json(ProductionEvent(type="heartbeat", task_id=task_id).model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.pop(socket, None)
