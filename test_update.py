"""Dev smoke test: call the backend's check_update() and print the result, to
verify the GitHub-release update check works against the current APP_VERSION.
Run directly: `python test_update.py`."""
import asyncio
from main import check_update

async def test():
    res = await check_update()
    print("Update check result:", res)

asyncio.run(test())
