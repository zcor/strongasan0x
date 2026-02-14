# Strong as an 0x

Open-source weekly fitness contest platform. Run community accountability challenges with AI-powered rankings.

## Features

- **Multi-platform attestations** — Collect weekly fitness reports via Discord, Telegram, or the web-based Warrior Dashboard
- **Strava integration** — Auto-generate attestation drafts from your Strava activities
- **AI-powered ranking** — Multiple AI providers (OpenAI, Anthropic, DeepSeek, Grok) rank weekly submissions
- **Warrior Dashboard** — Telegram-authenticated web interface for submitting and reviewing attestations
- **Admin review** — Review interface with hide/unhide controls and attestation detection backtesting
- **Discord role management** — Automatically assign roles based on weekly rankings

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/zcor/strongasan0x.git
cd strongasan0x
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Database

```bash
createdb rollcall  # PostgreSQL
python manage.py migrate
python manage.py createsuperuser
```

### 4. Run

```bash
python manage.py runserver        # Web interface
python manage.py run_discord_bot  # Discord bot
python manage.py run_telegram_bot # Telegram bot
```

## Weekly Workflow

1. **Friday-Sunday**: Participants submit attestations via Discord, Telegram, or Warrior Dashboard
2. **Monday**: Run AI ranking trials: `python manage.py run_ranking_trial`
3. **Publish**: Ingest and publish results: `python manage.py ingest_roll_call`
4. **Post**: Share rankings to Discord: `python manage.py post_rankings_to_discord`
5. **Roles**: Update Discord roles: `python manage.py assign_top_ten_role`

## Management Commands

| Command | Description |
|---------|-------------|
| `run_discord_bot` | Start the Discord bot |
| `run_telegram_bot` | Start the Telegram bot (polling or `--webhook`) |
| `run_ranking_trial` | Run AI-powered ranking of attestations |
| `ingest_roll_call` | Import published roll call data |
| `list_attestations` | View submitted attestations |
| `post_rankings_to_discord` | Post rankings to Discord channel |
| `assign_top_ten_role` | Update Discord roles based on rankings |
| `link_discord_user` | Map a Discord user to a contestant |
| `link_telegram_user` | Map a Telegram user to a contestant |
| `backtest_attestation_detection` | Validate detection logic against historical data |

## Architecture

- **`rollcall/`** — Main Django app (models, views, bot commands, services)
- **`strongasan0x/`** — Django project settings
- **`discord_dashboard/`** — Standalone Discord Intelligence Dashboard

## Configuration

See `.env.example` for all required environment variables.

Key settings in `strongasan0x/settings.py`:
- `ATTESTATION_MIN_LENGTH = 100` — Minimum attestation character count
- `ATTESTATION_WEEKEND_START_HOUR = 17` — Friday 5 PM UTC collection start
- `ATTESTATION_MULTI_PART_WINDOW_MINUTES = 15` — Multi-part grouping window

## License

MIT
