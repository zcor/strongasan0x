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

## CI and local validation

GitHub Actions spending is permanently `$0`. The one pull-request workflow is
supplemental evidence only while GitHub supplies the hosted run at no charge.
Do not add push or scheduled triggers, a paid runner, a payment method, or a
spending-cap increase. The hosted check runs `ruff check .` and the
Django-template-comment guard below.

When Actions cannot start because billing or quota blocks it before any step
runs, local validation at the exact candidate SHA is acceptable merge evidence.
Record the SHA and command output in the PR or merge record, then run:

```bash
git rev-parse HEAD
git status --short
git diff --check
ruff check .
if grep -rnE '\{#' --include='*.html' . | grep -vE '#\}'; then
  echo 'Multi-line Django template comment found. Make it single-line or use {% comment %}.'
  exit 1
fi
```

This fallback does not excuse a hosted job that actually started and failed.
Run Django tests only with an explicitly safe, non-production database
configuration; the local `.env` may point at shared infrastructure.

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
