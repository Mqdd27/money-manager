"""PostgreSQL connection helper."""

import os

import psycopg


def connect():
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(url)
