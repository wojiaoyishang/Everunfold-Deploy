import asyncio

from config_ui import Editor, SetupApp
from configure import configure
from storage import read


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
