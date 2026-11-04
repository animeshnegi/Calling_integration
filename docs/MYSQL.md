# MySQL database configuration

Telephony Control uses the single `DATABASE_URI` connection for customer accounts, extensions, numbers, providers, calls, recordings metadata, API keys, webhooks, billing, and invoices. Recording and voicemail audio remain in their dedicated Docker volumes.

Production startup requires MySQL. SQLite remains available only as a local test/development fallback when `DATABASE_URI` is empty.

## Existing Docker MySQL: network migration and DNS failures

An error like `Can't connect to MySQL server on '1Panel-mysql-H8JJ'
([Errno -2] Name or service not known)` means Docker DNS cannot resolve the
hostname. It does not by itself indicate a bad password or missing database.
Container hostnames resolve only on a shared user-defined Docker network.
The standalone base Compose file no longer implicitly joins a panel's network;
existing deployments that use a database container hostname must opt in.

Inspect only the network metadata (do not dump environment variables/secrets):

```bash
docker inspect 1Panel-mysql-H8JJ --format '{{json .NetworkSettings.Networks}}'
```

If MySQL is on `1panel-network`, copy `docker-compose.mysql-network.yml` from
this repository into the deployment folder and set these values in `.env`:

```dotenv
MYSQL_DOCKER_NETWORK=1panel-network
COMPOSE_FILE=docker-compose.yml:docker-compose.mysql-network.yml
```

The colon is the Compose file separator on Linux. If `COMPOSE_FILE` already
lists other overrides, keep those entries and append the MySQL override.
Leave `DATABASE_URI`, its password and its database name unchanged. Do not use
the container's temporary IP as a permanent database hostname.

Validate and recreate only the two database clients (brief application/worker
interruption; schedule around active calls):

```bash
docker compose config --quiet
docker compose up -d --no-build --no-deps telephony-ari telephony-api
docker compose logs --tail=50 telephony-api telephony-ari
curl -fsS http://127.0.0.1:5000/health
```

`COMPOSE_FILE` makes future plain `docker compose` commands and the deployment
helpers retain this attachment. Explicit `-f` flags take precedence: when using
those, include the database override too. Asterisk is not attached to the database
network, recreated or restarted by the command above. No image build, database
recreation, volume deletion, CRM connection or proxy installer is required.
The API/worker will regain connectivity only after DNS, authentication and
schema access all succeed; inspect the next error if startup still fails.

For immediate recovery **before** installing the persistent override, attach
both existing containers, only if they are not already on that network:

```bash
docker network connect 1panel-network telephony-api
docker network connect 1panel-network telephony-ari
docker compose restart telephony-ari telephony-api
```

These live attachments survive restarts, but not container replacement. Install
the override for a durable fix. Never run `docker compose down -v` to fix DNS.

## 1. Create the database and restricted user

Run as a MySQL administrator, changing the host restriction and password:

```sql
CREATE DATABASE eip_telephony
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'eip_app'@'YOUR_DOCKER_HOST_IP'
  IDENTIFIED BY 'replace-with-a-long-random-password';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
  ON eip_telephony.*
  TO 'eip_app'@'YOUR_DOCKER_HOST_IP';

FLUSH PRIVILEGES;
```

Do not grant global privileges, `DROP`, `FILE`, `PROCESS`, or administrative roles. After the schema is established, `ALTER` can be removed if your deployment policy applies future migrations separately; keep `CREATE` and `INDEX` only when automatic creation of new tables/indexes is desired.

## 2. Configure `.env`

```dotenv
DATABASE_URI=mysql+pymysql://eip_app:URL_ENCODED_PASSWORD@mysql.example.internal:3306/eip_telephony?charset=utf8mb4
```

The URI components are:

```text
mysql+pymysql://USERNAME:PASSWORD@HOST:PORT/DATABASE?charset=utf8mb4
```

Percent-encode reserved URI characters in the username or password. For example, `@` becomes `%40`, `:` becomes `%3A`, `/` becomes `%2F`, and `%` becomes `%25`. Generate an encoded value without putting it into shell history:

```bash
python3 -c 'import getpass,urllib.parse; print(urllib.parse.quote_plus(getpass.getpass("MySQL password: ")))'
```

The MySQL host must be reachable **from inside the Docker containers**. Do not use `localhost` unless MySQL runs in the same container (it normally should not). Use a private DNS name, private IP, or a Compose service name.

For a TLS-enabled MySQL endpoint, append supported PyMySQL SSL query parameters supplied by your database administrator, for example `&ssl_ca=/run/secrets/mysql-ca.pem`, and mount the CA file read-only into both API and ARI containers.

## 3. Local MySQL on Windows or macOS

Docker Desktop exposes the host to containers as `host.docker.internal`. When the telephony application runs in Docker, a local development URI can therefore be:

```dotenv
DATABASE_URI=mysql+pymysql://eip_local:URL_ENCODED_PASSWORD@host.docker.internal:3306/eip_telephony?charset=utf8mb4
```

The Compose file also maps `host.docker.internal` to Docker's host gateway for modern Linux Docker. If the Flask application itself runs directly in PowerShell rather than inside Docker, use `127.0.0.1` as the MySQL host instead.

The MySQL account must permit connections from Docker's network, not only `localhost`. Create a dedicated local account rather than using `root` with an empty password:

```sql
CREATE DATABASE IF NOT EXISTS eip_telephony
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'eip_local'@'%' IDENTIFIED BY 'local-development-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
  ON eip_telephony.* TO 'eip_local'@'%';
FLUSH PRIVILEGES;
```

`root@localhost` is a different MySQL identity from a connection arriving from a Docker network. Therefore `mysql+pymysql://root:@host.docker.internal/...` commonly fails with `Access denied`, even though an empty-password root login works from the host itself. This is a MySQL account/grant issue, not URI driver normalization.

Confirm that MySQL listens on an interface reachable from Docker. On a development machine this may require changing `bind-address` from `127.0.0.1` to `0.0.0.0` and restarting MySQL. Do not expose port 3306 publicly in production; restrict it with the host firewall/private network.

Test from a one-off Python process using the exact `.env` URI:

```bash
docker compose run --rm telephony-api python - <<'PY'
import os
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['DATABASE_URI'], pool_pre_ping=True)
with engine.connect() as connection:
    print(connection.execute(text('SELECT DATABASE(), CURRENT_USER(), VERSION()')).one())
PY
```

## 4. Automatic connection and table creation

At startup, both processes use SQLAlchemy's pooled MySQL engine with connection health checks. The application automatically creates all missing tables and indexes before handling work. This includes:

- `admin_users`, `settings`, `extensions`, `phone_numbers`, `sip_providers`;
- `calls`, recording metadata, and voicemail delivery state;
- `api_keys`, `api_idempotency`, webhook endpoints and deliveries;
- billing invoices and customer ownership columns.

The database itself and database user must already exist. The application intentionally does not create server-level users or grant permissions.

## 5. Verify connectivity

For an existing production deployment, validate the resolved Compose model and
recreate only the API and ARI worker after enabling the database network
override. Do not use an unscoped `docker compose up --force-recreate` for this
network-only repair: it can recreate Asterisk and interrupt active calls.

```bash
docker compose config --quiet
docker compose up -d --no-build --no-deps telephony-ari telephony-api
docker compose ps
docker compose logs --tail=100 telephony-api telephony-ari
curl -fsS http://127.0.0.1:5000/health
```

Verify from the API container without printing the URI:

```bash
docker compose exec telephony-api python - <<'PY'
import os
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['DATABASE_URI'], pool_pre_ping=True)
with engine.connect() as connection:
    print(connection.execute(text('SELECT DATABASE(), VERSION()')).one())
PY
```

Then inspect tables in MySQL:

```sql
USE eip_telephony;
SHOW TABLES;
SELECT COUNT(*) FROM admin_users;
SELECT COUNT(*) FROM calls;
```

## 6. Existing SQLite deployments

Setting `DATABASE_URI` changes the active database; it does not silently merge old SQLite rows. Back up `/app/instance/settings.db` and `/app/instance/calls.db` before switching. If the existing deployment contains production data, perform an explicit reviewed migration before activating MySQL. Never delete the `telephony_data`, recording, or voicemail volumes as part of the switch.

## 7. Backup

Use your existing MySQL backup system. A basic logical backup is:

```bash
mysqldump --single-transaction --routines --triggers \
  -h MYSQL_HOST -u eip_app -p eip_telephony \
  > eip_telephony-$(date +%F).sql
```

Back up the Asterisk recording and voicemail volumes separately because audio is not stored in MySQL.
