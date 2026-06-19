# --- app/services/ws_manager.py ---

import asyncio
from fastapi import WebSocket

class WebSocketManager:
    def __init__(self):
        self.connections: dict[str, WebSocket] = {}
        self.events: dict[str, asyncio.Event] = {}

    def register_task(self, task_id: str) -> asyncio.Event:
        """Регистрирует задачу и возвращает событие для ожидания подключения."""
        self.events[task_id] = asyncio.Event()
        return self.events[task_id]

    async def connect(self, websocket: WebSocket, task_id: str):
        """Обрабатывает подключение WebSocket клиента."""
        await websocket.accept()
        self.connections[task_id] = websocket
        if task_id in self.events:
            self.events[task_id].set() # Сигнализируем, что клиент подключился

    def disconnect(self, task_id: str):
        """Отключает клиента и очищает ресурсы."""
        if task_id in self.connections:
            del self.connections[task_id]
        if task_id in self.events:
            del self.events[task_id]

    async def send_message(self, message: str, task_id: str):
        """Отправляет сообщение конкретному клиенту по ID задачи."""
        if task_id in self.connections:
            try:
                await self.connections[task_id].send_text(message)
            except Exception:
                self.disconnect(task_id)

manager = WebSocketManager()