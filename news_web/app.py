import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .agent import ResearchAgent
from .archive_store import ArchiveStore
from .config import Settings


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class Filters(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    source: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=10)
    filters: Filters = Field(default_factory=Filters)

    @model_validator(mode="after")
    def total_length(self):
        if not self.message.strip() or sum(len(item.content) for item in self.history) > 60000:
            raise ValueError("消息为空或会话过长")
        return self


def create_app(settings=None, model_client=None):
    settings = settings or Settings.from_env()
    store = ArchiveStore(settings.archive_dir, settings.database_path, settings.report_archive_dir)
    agent = ResearchAgent(store, settings, model_client)
    slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.refresh, True)
        yield

    app = FastAPI(title="资讯之间 · Archive Intelligence", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model_ready": settings.model_ready}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        try:
            await asyncio.wait_for(slots.acquire(), timeout=1)
        except TimeoutError as exc:
            raise HTTPException(503, "当前问答较多，请稍后重试") from exc

        async def events():
            try:
                await asyncio.to_thread(store.refresh)
                async for item in agent.run(body.message, body.history, body.filters.model_dump(mode="json")):
                    if await request.is_disconnected():
                        break
                    data = json.dumps(item["data"], ensure_ascii=False)
                    yield f"event: {item['event']}\ndata: {data}\n\n"
            finally:
                slots.release()

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
        })

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        root = settings.static_dir.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if path and target.is_file():
            return FileResponse(target)
        if (root / "index.html").is_file() and (not path or "." not in path.rsplit("/", 1)[-1]):
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "网页尚未构建，请先运行前端构建命令")

    return app


app = create_app()
