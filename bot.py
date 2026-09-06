"""Gray Office Discord bot.

Forwards chat + document uploads to the grayoffice backend
(`POST <GRAYOFFICE_URL>/api/bots/ingest`), which does the AI routing, PDF->JSON
conversion and audit logging. This file focuses on a clean Discord experience:
rich embeds, a one-tap `/login` that auto-confirms, a `/upload` command for
invoices/receipts/JSON, and a friendly error prompt for anything that fails.
"""

import asyncio
import io
import json
import os
from typing import Callable, Optional

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: set GUILD_ID in .env for instant slash-command sync during development.
GUILD_ID = os.getenv("GUILD_ID")

GRAYOFFICE_URL = os.getenv("GRAYOFFICE_URL", "http://localhost:5173").rstrip("/")
BOT_INGEST_TOKEN = os.environ["BOT_INGEST_TOKEN"]

INGEST_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/ingest"
LINK_START_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/start"
LINK_STATUS_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/status"
LINK_REVOKE_ENDPOINT = f"{GRAYOFFICE_URL}/api/bots/link/revoke"

HEADERS = {"Authorization": f"Bearer {BOT_INGEST_TOKEN}"}
TIMEOUT = aiohttp.ClientTimeout(total=90)

# ---------------------------------------------------------------- look & limits

BRAND = 0x4F46E5  # Gray Office indigo
OK_COLOR = 0x22C55E
ERR_COLOR = 0xEF4444

PDF_EXTS = {".pdf"}
JSON_EXTS = {".json"}
ALLOWED_EXTS = PDF_EXTS | JSON_EXTS
MAX_BYTES = 25 * 1024 * 1024
MAX_FILES = 8
JSON_CONTEXT_LIMIT = 6000


class BackendError(Exception):
    """A non-200 from the backend, or an unreachable/garbled response."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def ext_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def actor(user: discord.abc.User) -> str:
    """Stable id used for account linking + the audit log."""
    return str(user.id)


# ------------------------------------------------------------------ backend I/O


async def _json_or_raise(resp: aiohttp.ClientResponse) -> dict:
    try:
        data = await resp.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
        body = (await resp.text())[:300]
        raise BackendError(f"Unexpected response ({resp.status}): {body}", resp.status)
    if resp.status != 200:
        msg = data.get("error") if isinstance(data, dict) else data
        raise BackendError(str(msg or f"HTTP {resp.status}"), resp.status)
    return data


async def call_ingest(
    session: aiohttp.ClientSession, text: str, external_user: str, files: list[dict]
) -> dict:
    payload = {
        "source": "discord",
        "externalUser": external_user,
        "text": text,
        "files": files,
    }
    try:
        async with session.post(INGEST_ENDPOINT, json=payload, headers=HEADERS) as resp:
            return await _json_or_raise(resp)
    except aiohttp.ClientError as e:
        raise BackendError(f"network error: {e}") from e
    except asyncio.TimeoutError as e:
        raise BackendError("the backend took too long to respond") from e


async def link_start(session: aiohttp.ClientSession, user: discord.abc.User) -> dict:
    payload = {"source": "discord", "externalUser": actor(user), "displayName": str(user)}
    async with session.post(LINK_START_ENDPOINT, json=payload, headers=HEADERS) as resp:
        return await _json_or_raise(resp)


async def link_status(session: aiohttp.ClientSession, user: discord.abc.User) -> dict:
    params = {"source": "discord", "externalUser": actor(user)}
    async with session.get(LINK_STATUS_ENDPOINT, params=params, headers=HEADERS) as resp:
        return await _json_or_raise(resp)


async def link_revoke(session: aiohttp.ClientSession, user: discord.abc.User) -> dict:
    payload = {"source": "discord", "externalUser": actor(user)}
    async with session.post(LINK_REVOKE_ENDPOINT, json=payload, headers=HEADERS) as resp:
        return await _json_or_raise(resp)


# ---------------------------------------------------------------------- embeds


def err_embed(title: str, desc: str, *, hint: Optional[str] = None) -> discord.Embed:
    e = discord.Embed(title=f"⚠️  {title}", description=desc[:4000], color=ERR_COLOR)
    if hint:
        e.add_field(name="Try this", value=hint[:1000], inline=False)
    return e


def backend_err_embed(exc: BackendError) -> discord.Embed:
    status = exc.status
    if status is None:
        return err_embed(
            "Can't reach Gray Office",
            "The backend didn't respond — it may be starting up or offline.",
            hint="Give it a moment and retry. If it persists, check `GRAYOFFICE_URL`.",
        )
    if status in (401, 403):
        return err_embed(
            "Not authorized",
            "The backend rejected the bot's ingest token.",
            hint="Make sure `BOT_INGEST_TOKEN` matches the backend's value.",
        )
    return err_embed(f"Backend error ({status})", str(exc))


# ---------------------------------------------------------- attachment handling


def sort_attachments(
    attachments: list[discord.Attachment],
) -> tuple[list[discord.Attachment], list[tuple[str, str]]]:
    """Partition into (accepted, rejected). Accepted is deduped, capped, and
    ordered PDFs-first so an upload batch never fails partway on a bad file."""
    accepted: list[discord.Attachment] = []
    rejected: list[tuple[str, str]] = []
    seen: set[tuple[str, int]] = set()

    for a in attachments:
        ext = ext_of(a.filename)
        key = (a.filename.lower(), a.size or 0)
        if ext not in ALLOWED_EXTS:
            rejected.append((a.filename, "unsupported type — only `.pdf` and `.json`"))
        elif a.size and a.size > MAX_BYTES:
            rejected.append(
                (a.filename, f"too large ({a.size / 1_048_576:.1f} MB · max 25 MB)")
            )
        elif key in seen:
            rejected.append((a.filename, "duplicate of another attachment"))
        else:
            seen.add(key)
            accepted.append(a)

    accepted.sort(key=lambda a: (ext_of(a.filename) not in PDF_EXTS, a.filename.lower()))
    for a in accepted[MAX_FILES:]:
        rejected.append((a.filename, f"over the {MAX_FILES}-file limit for one upload"))
    return accepted[:MAX_FILES], rejected


async def load_json_doc(session: aiohttp.ClientSession, a: discord.Attachment) -> str:
    async with session.get(a.url) as resp:
        if resp.status != 200:
            raise BackendError(f"couldn't download the file ({resp.status})")
        raw = await resp.read()
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BackendError(f"not valid JSON — {e}") from e
    return json.dumps(obj, indent=2, ensure_ascii=False)


def render_result(data: dict, *, source_name: Optional[str] = None) -> tuple[list, list]:
    """Turn a backend ingest response into (embeds, discord.File attachments)."""
    route = data.get("route")
    detail = data.get("detail")
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []

    if data.get("status") == "error":
        msg = detail.get("error") if isinstance(detail, dict) else detail
        embeds.append(
            err_embed(
                "Processing failed",
                str(msg or "unknown error"),
                hint="Check the file is a real invoice/receipt/document and try again.",
            )
        )
        return embeds, files

    if route == "ask" and isinstance(detail, dict) and detail.get("reply"):
        title = f"🧾  {source_name}" if source_name else "💬  Gray Office"
        embeds.append(
            discord.Embed(title=title, description=detail["reply"][:4000], color=BRAND)
        )
        return embeds, files

    if route == "pdf-to-json" and isinstance(detail, dict):
        results = detail.get("results") or []
        if not results:
            embeds.append(err_embed("No data extracted", "The backend returned nothing."))
        for r in results:
            fname = r.get("file", "document")
            pretty = json.dumps(r.get("json"), indent=2, ensure_ascii=False)
            e = discord.Embed(
                title=f"📄  {fname}",
                description="Extracted to structured JSON ✅",
                color=OK_COLOR,
            )
            preview = pretty if len(pretty) <= 1000 else pretty[:1000] + "\n…"
            e.add_field(name="Preview", value=f"```json\n{preview}\n```", inline=False)
            embeds.append(e)
            stem = os.path.splitext(fname)[0] or "document"
            files.append(
                discord.File(io.BytesIO(pretty.encode()), filename=f"{stem}.json")
            )
        return embeds, files

    embeds.append(
        discord.Embed(
            title=f"✅  Processed ({route})",
            description=(
                f"```json\n{json.dumps(detail, indent=2)[:1500]}\n```" if detail else "Done."
            ),
            color=OK_COLOR,
        )
    )
    return embeds, files


async def process_documents(
    session: aiohttp.ClientSession,
    attachments: list[discord.Attachment],
    note: Optional[str],
    user: discord.abc.User,
) -> tuple[list, list]:
    accepted, rejected = sort_attachments(attachments)
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []

    if rejected:
        lines = "\n".join(f"• **{n}** — {why}" for n, why in rejected)
        embeds.append(
            err_embed(
                f"Skipped {len(rejected)} attachment(s)",
                lines,
                hint="Re-upload as a PDF invoice/receipt or a `.json` document.",
            )
        )

    if not accepted:
        if not rejected:
            embeds.append(
                err_embed("Nothing to upload", "Attach at least one `.pdf` or `.json` file.")
            )
        return embeds, files

    external_user = actor(user)
    pdfs = [a for a in accepted if ext_of(a.filename) in PDF_EXTS]
    jsons = [a for a in accepted if ext_of(a.filename) in JSON_EXTS]

    # PDFs -> the backend's pdf-to-json route (batched in one call).
    if pdfs:
        payload = [
            {"name": a.filename, "url": a.url, "mime": a.content_type or "application/pdf"}
            for a in pdfs
        ]
        try:
            e, f = render_result(await call_ingest(session, note or "", external_user, payload))
            embeds += e
            files += f
        except BackendError as exc:
            embeds.append(backend_err_embed(exc))

    # JSON docs -> validated locally, then handed to the assistant as context.
    for a in jsons:
        try:
            pretty = await load_json_doc(session, a)
        except BackendError as exc:
            embeds.append(err_embed(f"Couldn't read {a.filename}", str(exc)))
            continue
        prompt = f"{note}\n\n" if note else ""
        prompt += f"Attached JSON document `{a.filename}`:\n```json\n{pretty[:JSON_CONTEXT_LIMIT]}\n```"
        if len(pretty) > JSON_CONTEXT_LIMIT:
            prompt += "\n(truncated)"
        if not note:
            prompt += "\n\nSummarise this document and flag anything that needs attention."
        try:
            e, f = render_result(
                await call_ingest(session, prompt, external_user, []), source_name=a.filename
            )
            embeds += e
            files += f
        except BackendError as exc:
            embeds.append(backend_err_embed(exc))

    return embeds, files


async def send_out(sender: Callable, embeds: list, files: list) -> None:
    """Deliver embeds (in groups of 5) + file attachments via any .send-like callable."""
    embeds = embeds or []
    files = files or []
    if not embeds and not files:
        return
    if not embeds:
        await sender(files=files[:10])
        return
    for i in range(0, len(embeds), 5):
        kwargs = {"embeds": embeds[i : i + 5]}
        if i == 0 and files:
            kwargs["files"] = files[:10]
        await sender(**kwargs)


# -------------------------------------------------------------------- the bot


class GrayOfficeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=TIMEOUT)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        print(f"Synced {len(synced)} command(s)")

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()


client = GrayOfficeBot()


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} (id: {client.user.id})")
    await client.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="/help · invoices & the books"
        )
    )


@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    original = getattr(error, "original", error)
    print("command error:", repr(original))
    embed = err_embed("Something went wrong", str(original)[:1000])
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


# ------------------------------------------------------------------- commands


@client.tree.command(name="ping", description="Check if the bot is responsive.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"🏓  Pong! `{round(client.latency * 1000)}ms`", color=BRAND
        )
    )


@client.tree.command(name="help", description="What this bot can do.")
async def help_cmd(interaction: discord.Interaction) -> None:
    e = discord.Embed(
        title="Gray Office — finance ops in Discord",
        description="Mention me or DM me anytime. Drag in a PDF and I'll extract it.",
        color=BRAND,
    )
    e.add_field(name="/ask `question`", value="Ask the finance assistant.", inline=False)
    e.add_field(
        name="/upload `file` … `note`",
        value="Send PDF invoices/receipts or JSON docs. PDFs come back as structured JSON.",
        inline=False,
    )
    e.add_field(
        name="/login · /whoami · /logout",
        value="Link a Gray Office account so everything is tied to you.",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


@client.tree.command(name="login", description="Connect your Gray Office account.")
async def login(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        data = await link_start(client.session, interaction.user)
    except BackendError as exc:
        await interaction.followup.send(embed=backend_err_embed(exc), ephemeral=True)
        return

    code, url = data["code"], data["url"]
    mins = round(data.get("expiresInSeconds", 900) / 60)
    embed = discord.Embed(
        title="🔗  Connect your Gray Office account",
        description=(
            "**1.** Open the link below and sign in.\n"
            "**2.** Enter this code:\n\n"
            f"# `{code}`\n\n"
            f"Expires in ~{mins} min. I'll update this message the moment you're connected."
        ),
        color=BRAND,
    )
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(label="Open Gray Office", url=url, style=discord.ButtonStyle.link)
    )
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)

    for _ in range(40):  # poll ~3.5 min
        await asyncio.sleep(5)
        try:
            status = await link_status(client.session, interaction.user)
        except BackendError:
            continue
        if status.get("linked"):
            who = status.get("name") or status.get("email") or "your account"
            done = discord.Embed(
                title="✅  Connected",
                description=(
                    f"You're linked to Gray Office as **{who}**.\n"
                    "Everything you send me now is tied to your account."
                ),
                color=OK_COLOR,
            )
            try:
                await interaction.followup.edit_message(msg.id, embed=done, view=None)
            except discord.HTTPException:
                pass
            return


@client.tree.command(name="whoami", description="Show which Gray Office account is connected.")
async def whoami(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        status = await link_status(client.session, interaction.user)
    except BackendError as exc:
        await interaction.followup.send(embed=backend_err_embed(exc), ephemeral=True)
        return
    if not status.get("linked"):
        await interaction.followup.send(
            embed=err_embed(
                "Not connected",
                "No Gray Office account is linked to your Discord user.",
                hint="Run `/login` to connect.",
            ),
            ephemeral=True,
        )
        return
    who = status.get("name") or status.get("email")
    await interaction.followup.send(
        embed=discord.Embed(
            title="✅  Connected", description=f"Linked to Gray Office as **{who}**.", color=OK_COLOR
        ),
        ephemeral=True,
    )


@client.tree.command(name="logout", description="Disconnect your Gray Office account.")
async def logout(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        data = await link_revoke(client.session, interaction.user)
    except BackendError as exc:
        await interaction.followup.send(embed=backend_err_embed(exc), ephemeral=True)
        return
    text = (
        "Disconnected from Gray Office."
        if data.get("revoked")
        else "You weren't connected to a Gray Office account."
    )
    await interaction.followup.send(
        embed=discord.Embed(description=text, color=BRAND), ephemeral=True
    )


@client.tree.command(name="ask", description="Ask the Gray Office finance assistant.")
@app_commands.describe(question="Your finance / books / invoice / GST question")
async def ask(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.defer(thinking=True)
    try:
        data = await call_ingest(client.session, question, actor(interaction.user), [])
    except BackendError as exc:
        await interaction.followup.send(embed=backend_err_embed(exc))
        return
    embeds, files = render_result(data)
    await send_out(interaction.followup.send, embeds, files)


@client.tree.command(
    name="upload", description="Upload PDF invoices/receipts or JSON docs to Gray Office."
)
@app_commands.describe(
    file="A PDF invoice/receipt or a JSON document",
    file2="Another file (optional)",
    file3="Another file (optional)",
    file4="Another file (optional)",
    note="Optional instructions, e.g. 'categorise as travel'",
)
async def upload(
    interaction: discord.Interaction,
    file: discord.Attachment,
    file2: Optional[discord.Attachment] = None,
    file3: Optional[discord.Attachment] = None,
    file4: Optional[discord.Attachment] = None,
    note: Optional[str] = None,
) -> None:
    await interaction.response.defer(thinking=True)
    attachments = [a for a in (file, file2, file3, file4) if a is not None]
    embeds, files = await process_documents(
        client.session, attachments, note, interaction.user
    )
    if not embeds and not files:
        embeds = [err_embed("Nothing happened", "No results came back from the backend.")]
    await send_out(interaction.followup.send, embeds, files)


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
        for token in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
            text = text.replace(token, "")
    text = text.strip()

    attachments = list(message.attachments)
    if not text and not attachments:
        return

    async with message.channel.typing():
        if attachments:
            embeds, files = await process_documents(
                client.session, attachments, text or None, message.author
            )
        else:
            try:
                embeds, files = render_result(
                    await call_ingest(client.session, text, actor(message.author), [])
                )
            except BackendError as exc:
                embeds, files = [backend_err_embed(exc)], []

    await send_out(message.reply, embeds, files)


if __name__ == "__main__":
    client.run(TOKEN)
