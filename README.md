# discord.grayoffice

Discord extension for Gray Office.

A discord.py bot that brings the Gray Office finance assistant into Discord.
Mention it, DM it, or use slash commands. It forwards messages and document
uploads to the backend at `POST <GRAYOFFICE_URL>/api/bots/ingest`, which does the
AI routing — free text is answered by the assistant, PDFs come back as structured
JSON — and replies with rich embeds.

## Commands

- `/ping` — check responsiveness.
- `/help` — what the bot can do.
- `/ask <question>` — query the Gray Office finance assistant.
- `/upload <file> [file2] [file3] [file4] [note]` — upload PDF invoices/receipts
  or JSON documents straight from Discord. Attachments are validated and sorted
  (PDFs first) so a batch never fails partway; anything unsupported, oversized, or
  duplicated is reported back in an error prompt instead of silently dropped.
  PDFs are returned as structured JSON (with the full result attached as a
  `.json` file); JSON docs are validated locally and summarised by the assistant.
- `/login` — connect your Gray Office account. The bot shows a code and a button
  to open Gray Office; once you enter the code there, the message updates itself
  to **✅ Connected** automatically. Linked messages are tied to your account
  (audit log + the assistant knows who it's talking to).
- `/whoami` — show which Gray Office account is connected.
- `/logout` — disconnect your Gray Office account.

You can also just drag a PDF into a DM with the bot, or attach one when you
mention it in a channel — same handling as `/upload`.

## Setup

1. Create a bot application at https://discord.com/developers/applications and
   copy its token. Enable the **Message Content Intent** in the Developer Portal.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Install dependencies:

   ```sh
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `GRAYOFFICE_URL`,
   and `BOT_INGEST_TOKEN` (must match grayoffice's `.dev.vars`). Optionally set
   `GUILD_ID` to a test server ID so slash commands sync instantly.

## Run

```sh
python bot.py
```

On start it prints how many slash commands synced. Type `/help` in any channel
the bot can see.
