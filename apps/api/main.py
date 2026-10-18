"""ASGI entrypoint: ``uvicorn apps.api.main:app``."""

from pulseops.api.app import create_app

app = create_app()
