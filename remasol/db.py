from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
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
    refund_destination_mode TEXT NOT NULL DEFAULT 'none',
    sender_ownership_verified INTEGER NOT NULL DEFAULT 0,
    client_ip TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payment_signature TEXT UNIQUE,
    payment_source_address TEXT,
    payment_lamports INTEGER,
    price_lamports INTEGER,
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

CREATE TABLE IF NOT EXISTS treasury_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    public_address TEXT NOT NULL UNIQUE,
    encrypted_private_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    max_payment_lamports INTEGER NOT NULL,
    daily_budget_lamports INTEGER NOT NULL,
    max_concurrent_demos INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    browser_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
    buyer_address TEXT NOT NULL UNIQUE,
    encrypted_private_key TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_lamports INTEGER,
    funding_lamports INTEGER,
    funding_signed_transaction TEXT,
    funding_signature TEXT UNIQUE,
    payment_signed_transaction TEXT,
    payment_signature TEXT UNIQUE,
    last_submit_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_demo_sessions_status ON demo_sessions(status);
CREATE INDEX IF NOT EXISTS idx_demo_sessions_created_at ON demo_sessions(created_at);

CREATE TABLE IF NOT EXISTS wallet_sessions (
    id TEXT PRIMARY KEY,
    browser_id TEXT NOT NULL,
    sender_address TEXT NOT NULL UNIQUE,
    encrypted_private_key TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    recipient_address TEXT,
    order_id TEXT REFERENCES orders(id),
    selected_lamports INTEGER,
    funding_lamports INTEGER,
    funding_signed_transaction TEXT,
    funding_signature TEXT UNIQUE,
    payment_signed_transaction TEXT,
    payment_signature TEXT UNIQUE,
    last_submit_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallet_sessions_status ON wallet_sessions(status);
CREATE INDEX IF NOT EXISTS idx_wallet_sessions_created_at ON wallet_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_wallet_sessions_order_id ON wallet_sessions(order_id);
CREATE INDEX IF NOT EXISTS idx_wallet_sessions_browser_id ON wallet_sessions(browser_id);
"""


DEMO_PRODUCT_SLUG = "the-digital-creator-s-guide"
DEMO_PRODUCT_NAME = "The Digital Creator's Guide"
DEMO_PRODUCT_DESCRIPTION = "A practical digital guide for creators who want to grow their online business."
DEMO_PRODUCT_PRICE_LAMPORTS = 2_000_000


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
        _migrate_wallet_browser_identity(connection)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "price_lamports" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN price_lamports INTEGER")
        if "refund_destination_mode" not in columns:
            connection.execute(
                "ALTER TABLE orders ADD COLUMN refund_destination_mode TEXT NOT NULL DEFAULT 'none'"
            )
        if "sender_ownership_verified" not in columns:
            connection.execute(
                "ALTER TABLE orders ADD COLUMN sender_ownership_verified INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """UPDATE orders
            SET price_lamports = (
                SELECT price_lamports FROM products WHERE products.id = orders.product_id
            )
            WHERE price_lamports IS NULL"""
        )
        connection.execute(
            """UPDATE orders SET refund_destination_mode = 'legacy_locked',
            sender_ownership_verified = 1
            WHERE refund_destination <> '' AND refund_destination_mode = 'none'"""
        )
        now = datetime.now(timezone.utc).isoformat()
        product = connection.execute(
            "SELECT id FROM products WHERE slug = ?", (DEMO_PRODUCT_SLUG,)
        ).fetchone()
        if product:
            connection.execute(
                "UPDATE products SET price_lamports = ? WHERE slug = ?",
                (DEMO_PRODUCT_PRICE_LAMPORTS, DEMO_PRODUCT_SLUG),
            )
        else:
            connection.execute(
                """INSERT INTO products
                (id, slug, name, description, price_lamports, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    DEMO_PRODUCT_SLUG,
                    DEMO_PRODUCT_NAME,
                    DEMO_PRODUCT_DESCRIPTION,
                    DEMO_PRODUCT_PRICE_LAMPORTS,
                    now,
                ),
            )
    finally:
        connection.close()


def _migrate_wallet_browser_identity(connection: sqlite3.Connection):
    """Allow one browser to keep a durable history of independent test wallets.

    The first Test Wallet release made ``browser_id`` unique, which meant a
    terminal session could not be followed by a new test. Rebuilding this
    single table keeps every existing row, private-key ciphertext, signature,
    and status while removing only that obsolete uniqueness constraint.
    """
    indexes = connection.execute("PRAGMA index_list(wallet_sessions)").fetchall()
    has_browser_unique = False
    for index in indexes:
        if not index["unique"]:
            continue
        columns = connection.execute(f'PRAGMA index_info("{index["name"]}")').fetchall()
        if [column["name"] for column in columns] == ["browser_id"]:
            has_browser_unique = True
            break
    if not has_browser_unique:
        return

    connection.execute(
        """CREATE TABLE wallet_sessions_migrated (
            id TEXT PRIMARY KEY,
            browser_id TEXT NOT NULL,
            sender_address TEXT NOT NULL UNIQUE,
            encrypted_private_key TEXT NOT NULL,
            client_ip TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            recipient_address TEXT,
            order_id TEXT REFERENCES orders(id),
            selected_lamports INTEGER,
            funding_lamports INTEGER,
            funding_signed_transaction TEXT,
            funding_signature TEXT UNIQUE,
            payment_signed_transaction TEXT,
            payment_signature TEXT UNIQUE,
            last_submit_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """INSERT INTO wallet_sessions_migrated
        (id, browser_id, sender_address, encrypted_private_key, client_ip, status,
         recipient_address, order_id, selected_lamports, funding_lamports,
         funding_signed_transaction, funding_signature, payment_signed_transaction,
         payment_signature, last_submit_at, last_error, created_at, updated_at)
        SELECT id, browser_id, sender_address, encrypted_private_key, client_ip, status,
               recipient_address, order_id, selected_lamports, funding_lamports,
               funding_signed_transaction, funding_signature, payment_signed_transaction,
               payment_signature, last_submit_at, last_error, created_at, updated_at
        FROM wallet_sessions"""
    )
    connection.execute("DROP TABLE wallet_sessions")
    connection.execute("ALTER TABLE wallet_sessions_migrated RENAME TO wallet_sessions")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_wallet_sessions_status ON wallet_sessions(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_wallet_sessions_created_at ON wallet_sessions(created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_wallet_sessions_order_id ON wallet_sessions(order_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_wallet_sessions_browser_id ON wallet_sessions(browser_id)")


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
