import asyncio

from config_ui import ACTIONS, Editor, SetupApp
from configure import configure
from storage import read
from textual.widgets import DataTable, Input, OptionList


def test_save_stays_open_and_editable(configured):
    store = configured()

    async def run():
        app = SetupApp(configure(store))
        async with app.run_test(size=(130, 46)) as pilot:
            await pilot.click("#save")
            await pilot.pause()
            assert app.saved
            assert not app._exit
            assert "保存成功" in "\n".join(app.query_one("#checks").transcript)
            await pilot.click("#edit")
            await pilot.pause()
            assert isinstance(app.screen, Editor)
            await pilot.press("escape")

    asyncio.run(run())
    assert read(store.path)["component"] == "everunfold-deploy"


def test_celery_pool_and_strategy_are_lists(configured):
    store = configured("docker")
    config = configure(store)

    async def run():
        app = SetupApp(config)
        async with app.run_test(size=(86, 28)) as pilot:
            table = app.query_one(DataTable)
            for key in ("pool", "worker_strategy"):
                table.move_cursor(
                    row=next(i for i, item in enumerate(config.fields) if item.key == key)
                )
                app.action_edit()
                await pilot.pause()
                assert app.screen.query_one(OptionList).highlighted == 0
                assert not app.screen.query(Input)
                await pilot.press("down", "enter")
                await pilot.pause()
            assert config.get("worker_strategy") == "auto"
            assert not config.errors()
            config.save()

    asyncio.run(run())
    backend = read(store.path)["backend"]["environment"]
    assert backend["WORKER_POOL"] == "prefork"
    assert backend["WORKER_AUTOSCALE"] == "4,1"


def test_sync_does_not_apply_unsaved_settings(configured):
    store = configured()
    saved = store.path.read_bytes()
    config = configure(store)
    field = next(item for item in config.fields if item.key == "database")
    changed_database = "postgresql+asyncpg://test:test@127.0.0.1:5433/test"
    config.edit(field, changed_database)

    async def run():
        app = SetupApp(config)
        async with app.run_test(size=(130, 46)) as pilot:
            await pilot.click("#sync")
            await pilot.pause()
            assert app.return_value == ACTIONS["sync"]

    asyncio.run(run())
    assert store.path.read_bytes() == saved
    assert config.get("database") == changed_database


def test_docker_quick_actions_fit_and_restart_leaves_draft_unsaved(configured):
    store = configured("docker")
    saved = store.path.read_bytes()
    config = configure(store)
    field = next(item for item in config.fields if item.key == "database")
    database = "postgresql+asyncpg://test:test@127.0.0.1:5433/test"
    config.edit(field, database)

    async def run():
        app = SetupApp(config)
        async with app.run_test(size=(86, 28)) as pilot:
            for action in (
                "rebuild-frontend",
                "start",
                "restart",
                "build-plugins",
                "hot-update-frontend",
            ):
                assert app.query_one("#" + action).region.bottom < 28
            await pilot.click("#restart")
            await pilot.pause()
            assert app.return_value == ACTIONS["restart"]

    asyncio.run(run())
    assert store.path.read_bytes() == saved
    assert config.get("database") == database
