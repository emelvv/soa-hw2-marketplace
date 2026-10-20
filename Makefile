.PHONY: generate up down test
generate:
	python scripts/generate.py

up:
	docker-compose up --build -d

down:
	docker-compose down

test:
	python3 scripts/smoke.py
