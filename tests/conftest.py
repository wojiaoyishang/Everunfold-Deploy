import pytest
from configure import configure
from storage import Store


@pytest.fixture
def configured(tmp_path):
    def create(mode="native"):
        store = Store(tmp_path / "Deploy/config.yaml")
        form = configure(store, mode)
        values = {
            "database": "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
            "backend_source": str(tmp_path / "Backend"),
            "frontend_source": str(tmp_path / "Frontend"),
            "backend_data": str(tmp_path / "BackendData"),
            "frontend_data": str(tmp_path / "FrontendData"),
            "auto_pull": "0",
        }
        for key, value in values.items():
            form.edit(next(item for item in form.fields if item.key == key), value)
        assert not form.errors(), form.errors()
        form.save()
        return store

    return create
