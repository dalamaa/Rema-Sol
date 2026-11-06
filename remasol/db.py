from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_lamports INTEGER NOT NULL CHECK(price_lamports > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    payment_address TEXT NOT NULL UNIQUE,
    encrypted_private_key TEXT NOT NULL,
    claim_code_hash TEXT NOT NULL,
    refund_destination TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payment_signature TEXT UNIQUE,
    payment_source_address TEXT,
    payment_lamports INTEGER,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
    amount_lamports INTEGER NOT NULL CHECK(amount_lamports > 0),
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    signed_transaction TEXT,
    transaction_signature TEXT UNIQUE,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_refunds_status ON refunds(status);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        db_path,
        timeout=10,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def init_db(app):
    connection = connect(app.config["DATABASE"])
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def begin_immediate(connection: sqlite3.Connection):
    connection.execute("BEGIN IMMEDIATE")


def commit(connection: sqlite3.Connection):
    connection.execute("COMMIT")


def rollback(connection: sqlite3.Connection):
    if connection.in_transaction:
        connection.execute("ROLLBACK")
