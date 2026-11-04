# Docker image/container names and safe upgrades

Compose defaults use the established EngineerIP image and container names so
local development and the existing production VM use the same names without
requiring extra `.env` entries:

| Service | Image repository | Container |
|---|---|---|
| API | `engineerip/telephony-api` | `engineerip-telephony-api` |
| Event worker | `engineerip/telephony-api` | `engineerip-telephony-ari` |
| Asterisk | `engineerip/asterisk` | `engineerip-asterisk` |

These image names are local tags; they are not a promise that images are
published in a public registry. Build/export the images on your workstation
and load them on the server. `IMAGE_TAG` supplies the tag (default `latest`).
The `TELEPHONY_*_IMAGE` and `TELEPHONY_*_CONTAINER` variables remain optional
overrides only when a deployment intentionally uses different names.

## Existing production server: retain the current names first

**Do not replace a running deployment's names just to upgrade the UI.** The
Compose defaults now match the existing EngineerIP image/container names, so
these five naming overrides are no longer required in the server `.env`. Keep
the existing Docker MySQL settings:

```dotenv
MYSQL_DOCKER_NETWORK=1panel-network
COMPOSE_FILE=docker-compose.yml:docker-compose.local.yml
```

The `COMPOSE_FILE` value in `.env` automatically selects both Compose files. Validate with:

```bash
docker compose config --quiet
```

Keep the existing database URI, credentials, `IMAGE_TAG`, Compose project name,
working directory and volume names. Preserve any additional overrides already
in `COMPOSE_FILE`. On Windows Compose uses `;` instead of `:` as the file
separator. When `COMPOSE_FILE` is set in `.env`, every `docker compose` command automatically uses both files. Do not add a third override file.

Build/export images using the default Compose names. `build-export-images.sh`
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

## Optional custom naming migration

Changing away from the EngineerIP defaults is optional and should be done
separately from troubleshooting or a UI upgrade. It requires a full maintenance
window: Asterisk replacement ends active calls.

1. Back up MySQL, Compose/`.env` and the existing named volumes. Keep the old
   images for rollback. Record the current project label with
   `docker inspect engineerip-asterisk --format '{{index .Config.Labels "com.docker.compose.project"}}'`.
   Retain that exact project name (`-p` if needed) and deployment directory.
2. Build/load the neutral image tags at the intended `IMAGE_TAG`; verify they
   exist locally before changing Compose. Do not rely on pulling public images.
3. Stop the old services using the existing Compose configuration, without deleting
   volumes: `docker compose stop`. Never use `down -v`, volume prune, or database
   recreation for a naming migration.
4. Set the desired `TELEPHONY_*_IMAGE` and `TELEPHONY_*_CONTAINER` overrides in
   `.env`. Retain `MYSQL_DOCKER_NETWORK` and `COMPOSE_FILE` when using Docker MySQL.
   Validate the config and verify the target images exist locally before starting.
5. Run `docker compose up -d --no-build` with the same project. Review Compose's
   recreation plan and verify that the original volume names are reused.
6. If nginx uses `engineerip-asterisk:8088`, update the proxy upstream only if
   the Asterisk container name was changed. Persist the proxy's shared network;
   do not expose ARI publicly.
7. Check API health, database records, `/ws`, registration, inbound/outbound
   calls, voicemail and recording playback before accepting traffic.

Rollback: stop the new stack (no volume deletion), restore the legacy image/container
overrides and old `IMAGE_TAG`, recreate with the same project and verify the proxy's old
upstream. Restoring a database backup is a separate operation, not automatically
required for a naming-only rollback.

## Internal compatibility identifiers are not product branding

`engineerip` still exists in the Stasis application name, voicemail/dialplan
contexts, some packaged audio paths and `X-EngineerIP-*` webhook headers. Renaming
these would affect stored mailboxes, live routing or API consumers. They are
intentionally retained, documented compatibility identifiers—not Docker/CRM
connections. This migration does not change them or the calling state machine.
