css:
	npm install && npm run css

test:
	python -m pytest -q

compile:
	python -m compileall app

docker-check:
	docker compose config
