# VPS deployment

This is a production-style starting point for a single VPS. It does not deploy itself and it intentionally leaves secrets out of the repository.

1. Create a dedicated service user and directories:

   ```bash
   sudo useradd --system --home /srv/remasol --shell /usr/sbin/nologin remasol
   sudo install -d -o remasol -g remasol /srv/remasol /var/lib/remasol /etc/remasol
   ```

2. Copy the project to `/srv/remasol`, create the virtual environment, and install dependencies:

   ```bash
   sudo -u remasol python3.11 -m venv /srv/remasol/.venv
   sudo -u remasol /srv/remasol/.venv/bin/pip install -r /srv/remasol/requirements.txt
   ```

3. Create `/etc/remasol/remasol.env` with mode `600`, owned by root. Generate values locally; never commit them:

   ```bash
   sudo sh -c 'umask 077; touch /etc/remasol/remasol.env; chown root:root /etc/remasol/remasol.env'
   sudoedit /etc/remasol/remasol.env
   ```

   Required values:

   ```dotenv
   REMA_SOL_SECRET_KEY=<long-random-session-secret>
   REMA_SOL_WALLET_KEY=<Fernet.generate_key() value>
   REMA_SOL_MERCHANT_USERNAME=merchant
   REMA_SOL_MERCHANT_PASSWORD_HASH=<werkzeug generate_password_hash output>
   REMA_SOL_DATABASE=/var/lib/remasol/remasol.sqlite3
   SOLANA_RPC_URL=https://api.devnet.solana.com
   REMA_SOL_COOKIE_SECURE=1
   REMA_SOL_TRUST_PROXY=1
   RATE_LIMIT_ENABLED=1
   ```

4. Install and start Gunicorn:

   ```bash
   sudo install -m 0644 deploy/remasol.service /etc/systemd/system/remasol.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now remasol
   sudo journalctl -u remasol -f
   ```

5. Put Nginx in front of Gunicorn. Replace `remasol.example.com` in `deploy/nginx-remasol.conf`, install it under `/etc/nginx/sites-available/remasol`, symlink it into `sites-enabled`, then obtain a certificate:

   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   sudo certbot --nginx -d remasol.example.com
   ```

   Keep port `8000` bound to `127.0.0.1`; only Nginx should be internet-facing.

6. Back up `/var/lib/remasol/remasol.sqlite3` and `/etc/remasol/remasol.env` securely. The environment file contains the key that decrypts checkout wallets; losing it makes stored wallet keys unrecoverable, while exposing it compromises them.

7. Keep `SOLANA_RPC_URL` on Devnet for the hackathon. Do not change it to mainnet without a separate production security review.
