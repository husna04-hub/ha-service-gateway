PYTHON ?= python3

.PHONY: install test check run drill load docker-up docker-down clean

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

check:
	$(PYTHON) -m ha_gateway --config config/gateway.yaml --check-config

run:
	$(PYTHON) -m ha_gateway --config config/gateway.yaml

drill:
	$(PYTHON) -m ha_gateway.drill

load:
	$(PYTHON) scripts/load_test.py --url http://127.0.0.1:8080

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf artifacts .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
