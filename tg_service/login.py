"""Interactive one-time login that creates the Telethon session file."""
import asyncio

from .tg.client import make_client


async def main():
    client = make_client()
    await client.start()  # asks phone / code / 2FA password in the terminal
    me = await client.get_me()
    print(f"logged in as {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
