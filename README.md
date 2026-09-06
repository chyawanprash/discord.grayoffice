# discord.grayoffice

discord extension for grayoffice

A minimal discord.py bot that responds to the `/ping` slash command.

## Setup

1. Create a bot application at https://discord.com/developers/applications and copy its token.
2. Invite the bot to your server with the `applications.commands` scope.
3. Install dependencies:

   ```sh
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`. Optionally set
   `GUILD_ID` to a test server ID so slash commands sync instantly (global sync
   can take up to an hour).

## Run

```sh
python bot.py
```

Then type `/ping` in any channel the bot can see; it replies `Pong! (<latency>ms)`.
