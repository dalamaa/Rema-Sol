import os

from remasol import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("REMA_SOL_HOST", "127.0.0.1"),
        port=app.config.get("PORT", 5007),
        debug=app.config.get("DEBUG", False),
    )
