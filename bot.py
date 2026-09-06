import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: set GUILD_ID in .env for instant slash-command sync during development.
GUILD_ID = os.getenv("GUILD_ID")

# grayoffice backend — bots POST normalized events to /api/bots/ingest and the
# backend does the AI routing / PDF->JSON / audit logging, then returns the result.
GRAYOFFICE_URL = os.getenv("GRAYOFFICE_URL", "http://localhost:5173").rstrip("/")
BOT_INGEST_TOKEN = os.environ["BOT_INGEST_TOKEN"]

INGEST_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/ingest"
LINK_START_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/start"
LINK_STATUS_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/status"
LINK_REVOKE_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/revoke"

HEADERS = {"Authorization": f"Bearer {BOT_INGEST_TOKEN}"}
TIMEOUT = aiohttp.ClientTimeout(total=60)


def _actor(user: discord.abc.User) -> tuple[str, str]:
    """Stable id used for account linking + a human label for the audit log."""
    return str(user.id), f"{user} ({user.id})"


async def ingest(text: str, external_user: str, files: list[dict]) -> str:
    """Forward a message to grayoffice and return a human-readable reply."""
    payload = {
        "source": "discord",
        "externalUser": external_user,
        "text": text,
        "files": files,
    }
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(INGEST_ENDPOINT, json=payload, headers=HEADERS) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"Backend error ({resp.status}): {data.get('error', data)}"
    return format_reply(data)


def format_reply(data: dict) -> str:
    detail = data.get("detail")
    if data.get("route") == "ask" and isinstance(detail, dict) and detail.get("reply"):
        return detail["reply"]
    if data.get("status") == "error":
        return f"Sorry, that failed: {detail}"
    if isinstance(detail, (dict, list)):
        import json

        return f"`{data.get('route')}` result:\n```json\n{json.dumps(detail, indent=2)[:1800]}\n```"
    return str(detail or f"Processed ({data.get('route')}).")


# ------------------------------------------------------------------ account linking


async def link_start(user: discord.abc.User) -> str:
    external_user, display = _actor(user)
    payload = {"source": "discord", "externalUser": external_user, "displayName": str(user)}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(LINK_START_ENDPOINT, json=payload, headers=HEADERS) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"Couldn't start linking ({resp.status}): {data.get('error', data)}"
    mins = round(data.get("expiresInSeconds", 900) / 60)
    return (
        "**Connect your Gray Office account**\n"
        f"1. Open {data['url']} and sign in.\n"
        f"2. Enter this code: **{data['code']}**\n\n"
        f"The code expires in about {mins} minutes. Run `/logout` here to disconnect later."
    )


async def link_status(user: discord.abc.User) -> str:
    external_user, _ = _actor(user)
    params = {"source": "discord", "externalUser": external_user}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(LINK_STATUS_ENDPOINT, params=params, headers=HEADERS) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"Couldn't check status ({resp.status}): {data.get('error', data)}"
    if not data.get("linked"):
        return "Not connected to a Gray Office account. Run `/login` to connect."
    who = data.get("name") or data.get("email")
    return f"Connected to Gray Office as **{who}**."


async def link_revoke(user: discord.abc.User) -> str:
    external_user, _ = _actor(user)
    payload = {"source": "discord", "externalUser": external_user}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(LINK_REVOKE_ENDPOINT, json=payload, headers=HEADERS) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"Couldn't disconnect ({resp.status}): {data.get('error', data)}"
    return (
        "Disconnected from Gray Office."
        if data.get("revoked")
        else "You weren't connected to a Gray Office account."
    )


class GrayOfficeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = GrayOfficeBot()


@client.tree.command(name="ping", description="Check if the bot is responsive.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(client.latency * 1000)
    await interaction.response.send_message(f"Pong! ({latency_ms}ms)")


@client.tree.command(name="login", description="Connect your Gray Office account.")
async def login(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(await link_start(interaction.user), ephemeral=True)


@client.tree.command(name="logout", description="Disconnect your Gray Office account.")
async def logout(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(await link_revoke(interaction.user), ephemeral=True)


@client.tree.command(name="whoami", description="Show which Gray Office account is connected.")
async def whoami(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(await link_status(interaction.user), ephemeral=True)


@client.tree.command(name="ask", description="Ask the Gray Office finance assistant.")
async def ask(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.defer(thinking=True)
    external_user, _ = _actor(interaction.user)
    reply = await ingest(question, external_user, [])
    await interaction.followup.send(reply[:2000])


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} (id: {client.user.id})")


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    is_dm = message.guild is None
    mentioned = client.user in message.mentions
    if not is_dm and not mentioned:
        return

    text = message.content
    if client.user:
        text = text.replace(f"<@{client.user.id}>", "").strip()

    files = [
        {"name": a.filename, "url": a.url, "mime": a.content_type or ""}
        for a in message.attachments
    ]
    if not text and not files:
        return

    external_user, _ = _actor(message.author)
    async with message.channel.typing():
        reply = await ingest(text, external_user, files)
    await message.reply(reply[:2000])


if __name__ == "__main__":
    client.run(TOKEN)
