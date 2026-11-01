"""Ponto de entrada (compatível com `uvicorn app:app` da v0.2).

A implementação da v0.2 (monolítica) foi modularizada na v0.3; a cópia original está em backup_v0.2/app.py.
"""
from api.main import create_app

app = create_app()
