import asyncio
from main import check_update

async def test():
    res = await check_update()
    print("Update check result:", res)

asyncio.run(test())
