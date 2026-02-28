"""Quick verification script: connect Discord bot, send test message, exit."""

import asyncio
import sys
from pathlib import Path

# Add src to path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import discord
from agentic_scraper.config import AppConfig


async def verify() -> None:
    config = AppConfig()

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"[OK] Bot connected as: {client.user}")
        print(f"[OK] Bot is in {len(client.guilds)} server(s):")
        for guild in client.guilds:
            print(f"     - {guild.name} (id: {guild.id})")

        # Try to find the deals channel
        channel = client.get_channel(config.discord_deals_channel_id)
        if channel is None:
            print(f"[WARN] Could not find channel {config.discord_deals_channel_id}")
            print("       Bot may not have access, or channel ID is wrong.")
            print("       Available text channels:")
            for guild in client.guilds:
                for ch in guild.text_channels:
                    print(f"         #{ch.name} (id: {ch.id})")
        else:
            print(f"[OK] Found deals channel: #{channel.name}")

            # Send a test embed
            embed = discord.Embed(
                title="Agentic Web Scraper - Bot Verification",
                description=(
                    "The bot is connected and can send messages to this channel.\n\n"
                    "Available commands: `!watch`, `!unwatch`, `!watchlist`, "
                    "`!scan`, `!pause`, `!resume`, `!status`, `!search`, `!deals`, "
                    "`!sites`, `!logs`"
                ),
                color=discord.Colour.green(),
            )
            embed.set_footer(text="This is a test message. Bot is working correctly.")
            try:
                await channel.send(embed=embed)
                print("[OK] Test message sent to deals channel!")
            except discord.Forbidden:
                print("[FAIL] Bot lacks permission to send messages in that channel.")
            except Exception as exc:
                print(f"[FAIL] Could not send message: {exc}")

        # Disconnect after verification
        await client.close()

    try:
        await client.start(config.discord_bot_token)
    except discord.LoginFailure:
        print("[FAIL] Invalid bot token. Check DISCORD_BOT_TOKEN in .env")
    except Exception as exc:
        print(f"[FAIL] Connection error: {exc}")


if __name__ == "__main__":
    asyncio.run(verify())
