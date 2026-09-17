"""Database configuration; environment values override the local dotenv file."""
import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

ROOT = Path(__file__).resolve().parents[2]


def database_url() -> URL:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    raw = values.get("SUPPLYAGENT_DATABASE_URL")
    if not raw:
        raise RuntimeError("SUPPLYAGENT_DATABASE_URL is missing; configure .env or environment")
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError):
        raise ValueError("SUPPLYAGENT_DATABASE_URL is invalid") from None
    if url.get_backend_name() != "postgresql":
        raise ValueError("SupplyAgent requires PostgreSQL")
    return url.set(drivername="postgresql+psycopg")
