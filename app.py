"""Ponto de entrada ASGI (`uvicorn app:app`), usado por run_server.py.

A aplicação é montada por api.main.create_app().
"""
from api.main import create_app

app = create_app()
