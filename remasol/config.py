import os


class Config:
    PORT = int(os.environ.get("PORT", "5007"))
    RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1") == "1"
    CHECKOUT_EXPIRY_MINUTES = int(os.environ.get("CHECKOUT_EXPIRY_MINUTES", "60"))
    MAX_UNPAID_CHECKOUTS_PER_IP = int(
        os.environ.get("MAX_UNPAID_CHECKOUTS_PER_IP", "8")
    )
    REFUND_FEE_BUFFER_LAMPORTS = int(
        os.environ.get("REFUND_FEE_BUFFER_LAMPORTS", "10000")
    )
    DEMO_PRODUCT_SLUG = os.environ.get(
        "DEMO_PRODUCT_SLUG", "the-digital-creator-s-guide"
    )
    DEMO_MIN_PAYMENT_LAMPORTS = int(
        os.environ.get("DEMO_MIN_PAYMENT_LAMPORTS", "500000")
    )
    DEMO_MAX_PAYMENT_LAMPORTS = int(
        os.environ.get("DEMO_MAX_PAYMENT_LAMPORTS", "5000000")
    )
    DEMO_FUNDING_FEE_BUFFER_LAMPORTS = int(
        os.environ.get("DEMO_FUNDING_FEE_BUFFER_LAMPORTS", "100000")
    )
    DEMO_DAILY_BUDGET_LAMPORTS = int(
        os.environ.get("DEMO_DAILY_BUDGET_LAMPORTS", "100000000")
    )
    DEMO_MAX_CONCURRENT_DEMOS = int(
        os.environ.get("DEMO_MAX_CONCURRENT_DEMOS", "3")
    )
    MIN_CLAIM_ATTEMPTS_PER_MINUTE = int(
        os.environ.get("MIN_CLAIM_ATTEMPTS_PER_MINUTE", "12")
    )
    MERCHANT_USERNAME = os.environ.get("REMA_SOL_MERCHANT_USERNAME", "merchant")
    MERCHANT_PASSWORD_HASH = os.environ.get("REMA_SOL_MERCHANT_PASSWORD_HASH", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("REMA_SOL_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12
    TRUST_PROXY = os.environ.get("REMA_SOL_TRUST_PROXY", "0") == "1"
