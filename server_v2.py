"""Compatibility entry point for the AIFlow Web Agent Service V3."""

from aiflow_server.gateway import app
from aiflow_server.config import load_settings


if __name__ == "__main__":
    import uvicorn

    settings = load_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
