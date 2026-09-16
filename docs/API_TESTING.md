# Telephony API Server Testing

These tests verify the Flask API from inside the VPS/private Docker network without exposing port 5000 publicly.

## 1. Check containers

```bash
docker compose ps
```

Both `asterisk` and `telephony-api` should be running. Asterisk should be healthy before the API starts.

## 2. Check API health

From the telephony project directory:

```bash
docker compose exec telephony-api python -c "import requests; print(requests.get('http://127.0.0.1:5000/health', timeout=5).status_code); print(requests.get('http://127.0.0.1:5000/health', timeout=5).text)"
```

A healthy result is HTTP 200 with JSON similar to:

```json
{"asterisk":"reachable","ok":true}
```

## 3. Test from the CRM container

The CRM should use the private Docker DNS name:

```text
http://engineerip-telephony-api:5000
```

Example health check from a container attached to `crm-network`:

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

## 5. Start an outbound test call

Use an authorized test destination and a registered extension:

```bash
curl -i -X POST \
  http://engineerip-telephony-api:5000/api/v1/calls \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+18586763047","extension":"101"}'
```

The API should return HTTP 201 and a `call.call_id`.

Then watch Asterisk:

```bash
docker compose logs -f asterisk
```

The call should be originated through Asterisk and then the configured SIP provider.

## 6. Get the call

Replace `<call_id>` with the ID returned by the create-call response:

```bash
curl -i \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>
```

## 7. Hang up

```bash
curl -i -X POST \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>/hangup
```

## 8. Set a disposition

```bash
curl -i -X POST \
  http://engineerip-telephony-api:5000/api/v1/calls/<call_id>/disposition \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"disposition":"follow_up","notes":"API test"}'
```

## 9. Verify the API can reach ARI

```bash
docker compose exec telephony-api python -c "from app.config import Config; from app.asterisk import AsteriskClient; print(AsteriskClient(Config).health())"
```

Do not publish Asterisk ARI port 8088 just to make this test work. The API should reach ARI using the private Docker hostname `asterisk:8088`.

## 10. Run automated tests

On the build/development host:

```bash
python -m pytest -q
python -m compileall app
```

## Important limitation

The current `CallStore` is in-memory. A call record therefore does not survive a `telephony-api` container restart. For the initial integration this is acceptable for call-control testing, but production CRM deployment should move call-state persistence to the EngineerIP database or a dedicated persistent store before relying on historical call records after service restarts.
