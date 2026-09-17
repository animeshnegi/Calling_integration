# Telephony API Server Testing

These tests verify the Flask API from inside the VPS/private Docker network without exposing port 5000 publicly.

## 1. Check containers

```bash
docker compose ps
```

`asterisk` should be healthy. `telephony-ari` should be running and the API should be able to reach the shared readiness file.

## 2. Check API health

From the telephony project directory:

```bash
docker compose exec telephony-api python -c "import requests; r=requests.get('http://127.0.0.1:5000/health', timeout=5); print(r.status_code); print(r.text)"
```

A healthy result is HTTP 200 with JSON containing:

```json
{"ok":true,"asterisk":"reachable","ari_ready":true}
```

## 3. Test from the CRM container

The CRM should use the private Docker DNS name:

```text
http://engineerip-telephony-api:5000
```

Example:

```bash
curl http://engineerip-telephony-api:5000/health
```

## 4. Test authentication

Without the token:

```bash
curl -i http://engineerip-telephony-api:5000/api/v1/calls
```

Expected: HTTP 401.

With the token:

```bash
curl -i \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  http://engineerip-telephony-api:5000/api/v1/calls
```

Expected: HTTP 200.

## 5. Verify Asterisk PJSIP

```bash
docker compose exec asterisk asterisk -rx 'pjsip show transports'
docker compose exec asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec asterisk asterisk -rx 'pjsip show registrations'
docker compose exec asterisk asterisk -rx 'pjsip show contacts'
```

At minimum, the `transport-udp` object must exist. After bootstrap/admin synchronization, configured extensions and providers should also appear.

## 6. Start an outbound test call

Use an authorized test destination and a registered extension:

```bash
curl -i -X POST \
  http://engineerip-telephony-api:5000/api/v1/calls \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+18586763047","extension":"101"}'
```

The API should return HTTP 201 and a `call.call_id`.

Then watch both services:

```bash
docker compose logs -f asterisk telephony-ari
```

Expected lifecycle:

```text
API -> persist call -> employee extension rings
employee answers -> customer leg originates
customer answers -> bridge created -> optional recording starts
hangup -> recording finalized -> bridge destroyed -> call completed
```

## 7. Get the call

Replace `<call_id>` with the ID returned by the create-call response:

```bash
curl -i \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>
```

The response should show employee/customer channel IDs, bridge state, answer state and final duration after the call completes.

## 8. Hang up

```bash
curl -i -X POST \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>/hangup
```

## 9. Set a disposition

```bash
curl -i -X POST \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>/disposition \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"disposition":"follow_up","notes":"API test"}'
```

## 10. Verify ARI directly from the API container

```bash
docker compose exec telephony-api python -c "from app.config import Config; from app.asterisk_client import AsteriskClient; print(AsteriskClient(Config).health())"
```

Do not publish Asterisk ARI port 8088 just to make this test work. The API should reach ARI using `asterisk:8088` on the private Docker network.

## 11. Run automated tests

On the build/development host:

```bash
python -m pytest -q
python -m compileall app
```

Validate Compose:

```bash
docker compose config
```

## 12. Restart/recovery test

During an active test call, restart only the ARI worker:

```bash
docker compose restart telephony-ari
```

The worker calls `recover_incomplete_calls()` on startup and reconciles persisted call state with live Asterisk channels. Verify that an employee-answered call still proceeds to the customer leg and that an already-connected call still reaches the bridge/completion lifecycle.

## Important boundary

Automated tests use fake Asterisk objects and cannot prove carrier registration, SIP NAT traversal, RTP audio, inbound DID delivery, Zoiper behavior, WebRTC microphone access or real IPComms connectivity. Those require the deployed VPS and live provider account.
