# Thin wrappers over the commands in the README, so there is one place to look.
PY ?= .venv/Scripts/python.exe

.PHONY: install db seed policies reset run test evals evals-holdout compare demo lint

install:
	python -m venv .venv
	$(PY) -m pip install -r requirements.txt

db:
	$(PY) -m alembic upgrade head

seed: db
	$(PY) -m scripts.seed_data

policies:
	$(PY) -m scripts.ingest_policies

reset:
	-rm -f support.db checkpoints.db
	$(MAKE) seed
	$(MAKE) policies

run:
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	$(PY) -m pytest tests

evals:
	$(PY) -m evals.run_evals

evals-holdout:
	$(PY) -m evals.run_evals --split holdout

compare:
	$(PY) -m evals.run_evals --compare --quiet

demo:
	$(PY) -m scripts.demo

lint:
	$(PY) -m ruff check app mock_services scripts evals tests
