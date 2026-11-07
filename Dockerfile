FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 remasol \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin remasol

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

# Copy only runtime sources. In particular, local .env files, databases,
# backups, wallets, tests, and Git metadata never enter the image.
COPY --chown=remasol:remasol app.py ./app.py
COPY --chown=remasol:remasol remasol ./remasol

RUN mkdir -p /app/instance /var/lib/remasol \
    && chown -R remasol:remasol /app/instance /var/lib/remasol

USER remasol
EXPOSE 5007

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5007/', timeout=4)"

CMD ["gunicorn", "--workers", "1", "--threads", "1", "--bind", "0.0.0.0:5007", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
