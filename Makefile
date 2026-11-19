test:
	python -m pytest -q

compile:
	python -m compileall app

docker-check:
	docker compose config

# Fast application/unit suite; does not require a MySQL server.
test-fast:
	python -m pytest -q --ignore=tests/integration

# Requires a disposable MySQL server; see docs/TESTING.md.
test-mysql:
	@test -n "$$TEST_MYSQL_URI" || (echo "Set TEST_MYSQL_URI to a dedicated test server"; exit 1)
	PYTHONPATH=. python -m pytest -q -ra tests/integration/test_mysql_live.py

test-proxy:
	@test -x "$${NGINX_BIN:-/usr/sbin/nginx}" || (echo "Install nginx or set NGINX_BIN"; exit 1)
	NGINX_BIN=$${NGINX_BIN:-/usr/sbin/nginx} python -m pytest -q tests/test_websocket_check.py tests/test_softphone_proxy_install.py
