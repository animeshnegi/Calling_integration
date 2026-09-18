# Logging, Rotation, and Disk Recovery

## What consumed the disk

Older deployments had two unbounded log paths:

1. Docker's default `json-file` container logs had no `max-size`/`max-file` policy.
2. Asterisk wrote `messages` and `security` files under `/var/log/asterisk` inside its writable container layer.

SIP scanning, registration errors, carrier retries, webhook failures, or repeated health errors can therefore grow for months. A 7 GB log is possible even when recordings are in separate named volumes.

## Permanent limits now included

Every telephony container has this Docker policy:

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
    compress: "true"
```

The maximum retained Docker output is approximately 30 MB per container, or approximately 90 MB for all three telephony containers.

Asterisk now writes only warning, error, and security events to the container console. It no longer writes `messages` or `security` files into its writable layer. `/var/log/asterisk` is also a 32 MB tmpfs safety boundary and `/var/run/asterisk` is a 4 MB tmpfs.

These limits apply to logs only. They do not delete call recordings, voicemail, settings, or call history.

## Recover space on the currently deployed old version

First identify usage:

```bash
cd ~/Calling_integration
./scripts/cleanup-logs.sh
sudo du -xh /var/lib/docker --max-depth=2 2>/dev/null | sort -h | tail -30
docker system df -v
```

To truncate only the current telephony logs:

```bash
./scripts/cleanup-logs.sh --apply
```

The script asks for `YES`, truncates the three known container log files, and clears old files under `/var/log/asterisk`. It does **not** delete containers, images, databases, recordings, voicemail, or volumes.

If the script cannot use `sudo`, run it as a user permitted to inspect/truncate Docker log paths or use the recreate procedure below.

## Apply rotation to existing containers

Docker logging options are fixed when a container is created. Merely editing Compose does not update an existing container. After installing the new `docker-compose.yml`, recreate containers:

```bash
# Keep IMAGE_TAG set to an image that already exists locally.
IMAGE_TAG=v5 docker compose up -d --no-build --force-recreate
docker compose ps
```

Recreating containers removes their old writable layers and container log files but preserves named volumes. Never add `-v`.

Verify the configured policy:

```bash
docker inspect engineerip-asterisk \
  --format '{{json .HostConfig.LogConfig}}'
```

Expected values include `max-size:10m` and `max-file:3`.

## Deploy the reduced Asterisk logger configuration

The Compose limits and tmpfs work with the old image after recreation. To also reduce Asterisk output to warning/error/security level, build and deploy the latest images:

```bash
# Personal computer
./scripts/build-export-images.sh v6

# VM
./scripts/import-start-images.sh engineerip-telephony-v6.tar.gz v6
```

## Safe inspection commands

```bash
# Container stream sizes and paths
docker inspect --format '{{.Name}} {{.LogPath}}' \
  engineerip-telephony-api engineerip-telephony-ari engineerip-asterisk

# Named volume sizes; recordings and voicemail may legitimately be large
docker system df -v
sudo du -sh /var/lib/docker/volumes/* 2>/dev/null | sort -h | tail

# Bounded recent logs only
docker compose logs --tail=200 asterisk telephony-ari telephony-api
```

## Do not use these blindly

Do not run:

```bash
docker compose down -v
docker volume prune
docker system prune --volumes
```

Those commands can permanently delete settings, call metadata, recordings, voicemail, certificates, or other applications' data.

`docker image prune` and `docker builder prune` do not delete named telephony data volumes, but they can remove rollback images/build cache. Review `docker system df -v` first and use them only when you understand what will be removed.

## Host journal limits

If Docker logs are small but `/var/log/journal` is large, inspect systemd separately:

```bash
journalctl --disk-usage
```

Host journal retention is a VM-wide policy and should be configured by the server administrator. Do not indiscriminately delete journal files while diagnosing an incident.

## Monitoring recommendation

Alert before the disk is full. At minimum monitor:

- root filesystem percentage and free bytes;
- `asterisk_recordings` volume;
- `asterisk_voicemail` volume;
- Docker container writable size;
- webhook/SendGrid repeated failures;
- unusually high SIP authentication/security events.

A practical warning threshold is 75–80% disk utilization, with a critical alert at 90%.
