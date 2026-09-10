test:
	python -m pytest -q

compile:
	python -m compileall app

docker-check:
	docker compose config
