# Neutral Docker naming and safe upgrades

The default product is **Telephony Control**. New installations use:

| Service | Image repository | Container |
|---|---|---|
| API | `telephony-control/api` | `telephony-api` |
| Event worker | `telephony-control/api` | `telephony-ari` |
| Asterisk | `telephony-control/asterisk` | `telephony-asterisk` |

These are locally built image tags, not a promise that images are published in a
public registry. Build/export both images on your workstation and load them on
the server. Set `TELEPHONY_API_IMAGE` / `TELEPHONY_ASTERISK_IMAGE` in `.env` to
use your own repository names (without tags); `IMAGE_TAG` supplies the tag.
The export/finish helpers resolve the effective image through Compose.

## Existing production server: retain the current names first

**Do not replace a running deployment's names just to upgrade the UI.** Before
using the new base Compose file, copy `docker-compose.legacy-names.yml` onto the
server and add it to `COMPOSE_FILE` in `.env`. For your existing Docker MySQL:

```dotenv
MYSQL_DOCKER_NETWORK=1panel-network
COMPOSE_FILE=docker-compose.yml:docker-compose.legacy-names.yml:docker-compose.mysql-network.yml
```

Keep the existing database URI, credentials, `IMAGE_TAG`, Compose project name,
working directory and volume names. Preserve any additional overrides already
in `COMPOSE_FILE`. On Windows Compose uses `;` instead of `:` as the file
separator. Explicit `-f` options supersede `COMPOSE_FILE`; include all required
overrides if you use them.

Build the workstation images with the same legacy override enabled, or tag the
new image IDs with the old repository names before export. `build-export-images.sh`
exports the repositories selected by Compose. Load the resulting archive on the
server, then, in a maintenance window, recreate **only** the application services:

```bash
docker compose config --quiet
docker compose up -d --no-build --no-deps telephony-ari telephony-api
```

This reuses existing data and keeps the old Asterisk container in place. The
updated proxy installer defaults to the neutral Asterisk container name. If
proxy repair is actually needed on this legacy installation, pass the old name:

```bash
sudo python3 scripts/install-softphone-proxy.py --domain YOUR_WEB_DOMAIN \
  --asterisk-container engineerip-asterisk --network telephony-internal
# Review the dry run; only then repeat with --apply --connect-network if needed.
```

For an older private bridge, use `--network engineerip-telephony-internal` instead.
`finish-deployment.py` also accepts `--asterisk-container` and `--network`, but it
recreates the full stack: use it only in a maintenance window. For legacy log
cleanup, set `API_CONTAINER=engineerip-telephony-api`,
`ARI_CONTAINER=engineerip-telephony-ari` and `ASTERISK_CONTAINER=engineerip-asterisk`.
The audio diagnostic accepts the Asterisk container name as its first argument.

## Optional later migration to neutral names

Do this separately from troubleshooting or a UI upgrade. It requires a full
maintenance window: Asterisk replacement ends active calls.

1. Back up MySQL, Compose/`.env` and the existing named volumes. Keep the old
   images for rollback. Record the current project label with
   `docker inspect engineerip-asterisk --format '{{index .Config.Labels "com.docker.compose.project"}}'`.
   Retain that exact project name (`-p` if needed) and deployment directory.
2. Build/load the neutral image tags at the intended `IMAGE_TAG`; verify they
   exist locally before changing Compose. Do not rely on pulling public images.
3. Stop the old services using the **old configuration including the legacy
   override**, without deleting volumes: `docker compose stop`. Never use
   `down -v`, volume prune, or database recreation for a naming migration.
4. Remove only `docker-compose.legacy-names.yml` from `COMPOSE_FILE`. Keep the
   MySQL override and other topology settings. Validate `docker compose config --quiet`.
5. Run `docker compose up -d --no-build` with the same project. Review Compose's
   recreation plan and verify that the original volume names are reused.
6. If nginx uses `engineerip-asterisk:8088`, review the proxy installer dry run
   with `--asterisk-container telephony-asterisk --network telephony-internal`,
   then apply it. Persist the proxy's shared network. Do not expose ARI publicly.
7. Check API health, database records, `/ws`, registration, inbound/outbound
   calls, voicemail and recording playback before accepting traffic.

Rollback: stop the new stack (no volume deletion), restore the legacy override
and old `IMAGE_TAG`, recreate with the same project and verify the proxy's old
upstream. Restoring a database backup is a separate operation, not automatically
required for a naming-only rollback.

## Internal compatibility identifiers are not product branding

`engineerip` still exists in the Stasis application name, voicemail/dialplan
contexts, some packaged audio paths and `X-EngineerIP-*` webhook headers. Renaming
these would affect stored mailboxes, live routing or API consumers. They are
intentionally retained, documented compatibility identifiers—not Docker/CRM
connections. This migration does not change them or the calling state machine.
