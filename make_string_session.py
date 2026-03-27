from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio

API_ID = 31407058
API_HASH = "21e677e9e940fc2d20d3022d043d994d"

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()
    print("\nUSER_STRING_SESSION:\n")
    print(client.session.save())
    await client.disconnect()

asyncio.run(main())