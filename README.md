# discord.grayoffice

discord extension for grayoffice

A discord.py bot that forwards messages (mentions + DMs) and PDF attachments to
the grayoffice backend at `POST <GRAYOFFICE_URL>/api/bots/ingest`. The backend
does the AI routing — free text is answered by the Gray Office finance assistant,
PDFs come back as structured JSON — and the bot replies with the result.
`/ping` still checks responsiveness; `/ask <question>` queries the backend AI.

## Setup

1. Create a bot application at https://discord.com/developers/applications and copy its token.
2. Invite the bot to your server with the `applications.commands` scope.
3. Install dependencies:

   ```sh
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `GRAYOFFICE_URL`,
   and `BOT_INGEST_TOKEN` (must match grayoffice's `.dev.vars`). Optionally set
   `GUILD_ID` to a test server ID so slash commands sync instantly (global sync
   can take up to an hour). Enable the **Message Content Intent** for the bot in
   the Developer Portal so it can read message text.

## Run

```sh
python bot.py
```

Then type `/ping` in any channel the bot can see; it replies `Pong! (<latency>ms)`.
