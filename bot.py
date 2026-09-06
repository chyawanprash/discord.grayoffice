import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: set GUILD_ID in .env for instant slash-command sync during development.
GUILD_ID = os.getenv("GUILD_ID")


class PingBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = PingBot()


@client.tree.command(name="ping", description="Check if the bot is responsive.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(client.latency * 1000)
    await interaction.response.send_message(f"Pong! ({latency_ms}ms)")


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} (id: {client.user.id})")


if __name__ == "__main__":
    client.run(TOKEN)
