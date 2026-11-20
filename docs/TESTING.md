# Test and validation guide

## Setup and fast tests

Use Python 3.13 (the CI version), a virtual environment, and development dependencies:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
make test-fast
```

The fast suite uses independent temporary SQLite databases and fake telephony
clients. It includes signup takeover attempts, same-digit cross-tenant call and
webhook authorization, ownership snapshots, idempotency crash/retry behavior,
shared concurrent rate limits, and webhook destination/DNS policy tests.
It does not prove that a carrier call or MySQL migration works.

## Real MySQL acceptance tests

Provision a **dedicated disposable MySQL 8.4 test server**. Set `TEST_MYSQL_URI`
to a test user with CREATE/DROP DATABASE permission on that server:

```sh
export TEST_MYSQL_URI='mysql+pymysql://root:TEST_PASSWORD@127.0.0.1:3306/?charset=utf8mb4'
make test-mysql
```

Do not use production credentials or a production server. Each test creates a
random `eip_test_*` database and drops that database in teardown; the database
component in the supplied URI is not used. These tests exercise fresh schema
creation, concurrent initialization, upgrading a legacy extension table and call
ownership column, and real account/call/webhook/idempotency/rate-limit SQL.
Additional historical schema fixtures should be added as migrations evolve.

Without `TEST_MYSQL_URI`, these tests are explicitly skipped. CI has a dedicated
MySQL service job that supplies it; a passing SQLite run alone is not sufficient.

## Real nginx tests

```sh
# Debian/Ubuntu: sudo apt-get install nginx
NGINX_BIN=/usr/sbin/nginx make test-proxy
```

This target requires nginx rather than accepting a skipped real-proxy check.
The normal CI job installs nginx and executes the full Python suite. Local
`python -m pytest -q` can still skip these integration checks if nginx is absent.

## Live telephony / release gate

On a staging deployment using the production network layout, verify:

- Provider and device registration, inbound DID selection and tenant boundaries.
- Employee-first outbound calling, customer-leg origination and two-way RTP.
- IVR prompt expiry and fallback, including slow/unreachable webhook destinations.
- Recording completion/playback, voicemail delivery, and lifecycle webhooks.
- Worker/API restart during originate and during an idempotent retry.
- Trusted-proxy configuration, shared limits across workers, and egress firewall.
- Backup restore and an upgrade against a copy of the deployment's actual schema.

Never perform paid-call or destructive migration testing against customer data.

## Latest local regression verification — 2026-11-19

After personal voicemail greetings, workflow timer fixes, duplicate-call handling,
and compact console layouts:

- Full suite: **658 passed, 5 skipped** (162.29 seconds).
- Python compilation: all application and deployment-script modules passed.
- JavaScript syntax: all `web/**/*.js` files passed `node --check`.
- Shell syntax: shell scripts under `scripts/` and `asterisk/` passed `bash -n`.
- `git diff --check` passed.

Expanded greeting checks cover duration boundaries, phone temporary-greeting
precedence, reset without deleting messages, invalid-upload preservation,
authentication/CSRF/tenant isolation, administrator access, and independent
greetings for same-digit extensions on different numbers. A Node test verifies
the browser WAV writer's output format and PCM clipping using simulated Web Audio;
it does not exercise a real browser's decoder or microphone.

The five skips were three real-MySQL checks (no dedicated `TEST_MYSQL_URI`) and
two real-nginx checks (nginx unavailable). Docker/Asterisk, a real browser, and
physical SIP phones were unavailable here. These local results do **not** replace
the staging release gate above. Specifically, make two incoming calls to distinct
mailboxes, listen for each uploaded greeting, reset one to the automated greeting,
and confirm the other is unchanged. Verify two-way audio, ring/input timing,
one missed-call notification per call, recording playback, and voicemail email
on the deployed system before declaring end-to-end acceptance complete.
