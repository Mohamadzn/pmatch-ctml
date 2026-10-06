"""FastAPI entry point: uvicorn app.main:app --reload"""

from __future__ import annotations

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI

from app.api.routes.ctml import router as ctml_router

load_dotenv(find_dotenv(usecwd=True), override=False)  # .env in the folder uvicorn runs in, as the CLI

app = FastAPI(title="PMATCH CTML", version="0.1.0")
app.include_router(ctml_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
