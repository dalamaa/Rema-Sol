# RemaSol Docker deployment

This is the production-style single-instance deployment for the OVH VPS. It
uses Docker Compose, Gunicorn, a private bind-mounted SQLite directory, and the
existing host Nginx/Certbot installation. It does not use the Mac's database,
wallet key, treasury, or `.env` file.

## 1. Prepare the VPS directory

Run these commands on the VPS as `ubuntu`:

```bash
sudo install -d -m 0750 -o ubuntu -g ubuntu /opt/hackathon /opt/hackathon/remasol
cd /opt/hackathon/remasol
git clone https://github.com/dalamaa/Rema-Sol.git .
git checkout main
sudo install -d -m 0700 -o 10001 -g 10001 /opt/hackathon/remasol/data
```

If the checkout already exists, update it with `git pull --ff-only` instead of
cloning over it. Do not copy the Mac `.env`, SQLite database, backups, or wallet
files to this host.

## 2. Create fresh private settings

Create a fresh VPS-only environment file:

```bash
cd /opt/hackathon/remasol
cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

Set a new `REMA_SOL_SECRET_KEY`, a new `REMA_SOL_WALLET_KEY`, a merchant
username, and a Werkzeug password hash. Keep the single quotes around that
hash in `.env` when it contains `$` characters, so Docker Compose passes it
literally. Keep `REMA_SOL_DATABASE` set to
`/var/lib/remasol/remasol.sqlite3`, `REMA_SOL_COOKIE_SECURE=1`,
`REMA_SOL_TRUST_PROXY=1`, and `FLASK_DEBUG=0`. Keep `SOLANA_RPC_URL` on
Devnet. The wallet key must be unique to this VPS; it causes RemaSol to create a
separate Devnet treasury on first boot and never funds it automatically.

The environment file is private application state. Back it up together with
the `data/` directory; losing either the wallet key or its matching database
makes encrypted stored checkout keys unrecoverable.

## 3. Build and start one safe application instance

```bash
cd /opt/hackathon/remasol
docker compose -p remasol-hackathon build
docker compose -p remasol-hackathon up -d
docker compose -p remasol-hackathon ps
docker compose -p remasol-hackathon logs --tail=100 remasol
curl --fail --silent --show-error http://127.0.0.1:8507/ > /dev/null
```

Gunicorn runs one synchronous worker and one thread intentionally. RemaSol's
rate limiter is process-local, while SQLite `BEGIN IMMEDIATE` reservations and
the treasury execution lock are the payment-safety boundaries. A second worker
or concurrent execution path would not improve this single-instance checkout
flow.

The container is non-root, has a read-only root filesystem, and publishes only
`127.0.0.1:8507` on the host. The bind-mounted `data/` directory persists the
SQLite database, encrypted checkout/test-wallet keys, signed transactions,
refund records, and the separate hosted treasury. Container rebuilds and VPS
reboots do not erase them.

## 4. Add the two hostnames after registering them

Register one hostname with Dynu and one different hostname with DuckDNS. Do not
guess availability. Create an A record for each hostname pointing to the OVH
public IPv4 address.

Only after both names resolve, replace the placeholders in
`deploy/nginx-remasol.conf` with the exact names. Install it as a new Nginx
site; do not overwrite existing site files:

```bash
sudo install -m 0644 deploy/nginx-remasol.conf /etc/nginx/sites-available/remasol
sudo ln -s /etc/nginx/sites-available/remasol /etc/nginx/sites-enabled/remasol
sudo nginx -t
sudo systemctl reload nginx
```

The bootstrap template sends both names to the same private application
endpoint. It has no TLS listener until Certbot has issued the certificate, so
the existing Nginx configuration remains testable during setup.

Issue one SAN certificate for both names through the existing Certbot setup:

```bash
sudo certbot --nginx --redirect -d PRIMARY_HOSTNAME -d BACKUP_HOSTNAME
sudo certbot renew --dry-run
```

Certbot adds the HTTPS listener and keeps both hostnames in the same server
block, preserving each requested host rather than redirecting the backup name
to the primary. Its renewal timer remains responsible for automatic renewal.
Verify that Nginx still passes its full configuration test after issuance.

## 5. Operations and backup

```bash
cd /opt/hackathon/remasol
docker compose -p remasol-hackathon ps
docker compose -p remasol-hackathon logs --tail=200 remasol
docker compose -p remasol-hackathon restart remasol
```

Back up `data/` and `.env` as one protected unit. Do not print the environment
file, private wallet keys, or passwords in logs or deployment reports. Never
fund the treasury, request an airdrop, or broadcast a transaction as part of
deployment; use the public treasury address shown in the authenticated treasury
page when you decide to fund the Devnet demo manually.
