import os

import pytest

# Tests run fully offline: mock analyst + local hashing embeddings against a real
# Postgres with pgvector (DATABASE_URL must point at a disposable database).
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/aml_test")
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ.pop("API_KEY", None)


@pytest.fixture()
def seeded():
    from scripts.seed import seed

    seed(reset=True)
