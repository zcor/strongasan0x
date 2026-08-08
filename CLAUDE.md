# CLAUDE.md — strongasan0x

## Project Overview

Django-based weekly fitness contest platform ("Strong as an 0x"). Warriors submit weekly health attestations via Discord and Telegram bots, AI models rank them, and results are published on the self-hosted Roll Call archive with a Homeric ode.

- Remote: `git@github.com:zcor/strongasan0x.git` (branch: `main`)
- Runs on port 8001
- DB table prefix: `rollcall_*` (shared PostgreSQL with garmin_project's `garmin_data_*` tables)

## Concurrent Git safety

Other Claude, Codex, human, or automation sessions may be active in this
repository. Before any Git mutation, use `$git-session-safety`. Never claim or
clean another session's dirty/staged/untracked paths or registered worktree:
unknown ownership is preserved and reported. Stage and commit explicit owned
paths only; never use `git add -A`, `git add .`, or an unscoped commit in a
shared checkout.

Session close-out is not integration or publication. An integration owner must
re-check the current remote target and candidate lineage immediately before a
merge or push. A rejected push may be duplicate or superseded work; use
`git cherry` and end-state comparison before replaying it.

## Development Commands

```bash
python manage.py runserver 8001

# Weekly publishing — the full ordered workflow lives in two skills:
#   .agents/skills/roll-call-prep/     trials -> ode -> markdown -> staged ingest -> preview
#   .agents/skills/roll-call-publish/  publish live -> X -> Telegram -> Discord -> roles
# (garmin_project/WEEKLY_PROCESS.md is superseded: it describes the retired Substack flow)
python manage.py list_attestations
python manage.py run_ranking_trial --week-end YYYY-MM-DD --provider deepseek
python manage.py run_ranking_trial --week-end YYYY-MM-DD --output-only
python manage.py generate_substack_ode --week-end YYYY-MM-DD --provider deepseek --output ode.txt
python manage.py ingest_roll_call --week YYYY-MM-DD --substack-url URL --rankings 'JSON' --publish --overwrite
python manage.py post_rankings_to_discord --week YYYY-MM-DD
python manage.py generate_twitter_rankings --week YYYY-MM-DD
python manage.py assign_top_ten_role
python manage.py extract_metrics  # only processes is_published=True weeks

# Self-hosted post (Substack is retired — the URL field now stores the on-site link).
# Run these only through the Mini's Strong as an 0x wrapper; it loads this project's .env
# instead of inheriting unrelated shell credentials.
scripts/roll_call_runtime.sh preflight_roll_call_syndication --week-end YYYY-MM-DD  # after page is live; sends nothing
scripts/roll_call_runtime.sh post_rankings_to_x --week-end YYYY-MM-DD               # dry-run first
scripts/roll_call_runtime.sh post_rankings_to_telegram --week-end YYYY-MM-DD        # dry-run first

# LEGACY — do not use. publish_roll_call is unmaintained since 2026-02-14: it prompts
# interactively (EOFError in non-interactive shells), never posts to Telegram, only
# generates the tweet, and still asks for a Substack URL. Run the steps individually.
# python manage.py publish_roll_call --week-end YYYY-MM-DD --substack-url URL

# Bots
python manage.py run_discord_bot
python manage.py run_telegram_bot
```

## Architecture

### Core App (`rollcall/`)
- **models.py** — `Attestation`, `WeeklyRollCall`, `RankingTrial`, `RollCallRanking`, `ExtractedMetrics`, `TelegramUserMapping`, `DiscordUserMapping`
- **views.py** — Homepage, Hall of Champions, leaderboards. Champion score: `total_points * log2(1 + weeks) / sqrt(weeks)` where `rank_to_points(rank) = 11 - rank` for ranks 1-10
- **warrior/** — Authenticated warrior dashboard (progress charts, history, attestation editing). Admin preview via `?warrior=NAME` on `/warrior/progress/` (gated by `ADMIN_TELEGRAM_IDS`)
- **services/ai_ranking.py** — `call_deepseek()`, `call_anthropic()`, `call_openai()`, `call_grok()`, `parse_ranking_response()`
- **services/ranking_stats.py** — `calculate_cumulative_stats(trials)`, `check_convergence(trials)`, `normalize_name()`, `build_canonical_name_map()`

### Key Patterns
- Multi-part attestations: `parent_attestation` ForeignKey with 15-min auto-grouping window
- `--week` flag = Monday publication date (calculates previous week). `--week-end` = exact Sunday.
- Ranking convergence: min 3 trials, std error < 0.5, no statistical ties in top 5

## Known Gotchas

- **`call_deepseek()` returns `(model_name, response_text, cost_info)`** — NOT `(response_text, model, cost_info)`. Getting this wrong causes all trials to fail silently with "could not parse response".
- **`extract_metrics` skips unpublished weeks** — filters by `weekly_roll_call__is_published=True`. For unpublished weeks, call `extract_and_save(attestation)` directly in Django shell.
- **`run_ranking_trial` interactive prompt** — asks "Run another trial?" which causes EOFError in non-interactive shells. Pipe `echo "n"` for single trials, or write a custom shell loop for batch.
- **Django `timezone.utc` doesn't exist** — use `datetime.timezone.utc` or `from datetime import timezone; timezone.utc`.
- **Anthropic credits frequently depleted** — always try `--provider deepseek` as fallback.
- **DeepSeek ode generation** — needs explicit rhyming instructions (AA BB CC couplet pattern) in the prompt or it produces prose/blank verse. The `ODE_PROMPT_TEMPLATE` was updated with "CRITICAL REQUIREMENT — RHYMING" section and example stanzas.
- **Substack markdown line breaks** — verse lines need trailing double-spaces (`  `) for proper rendering. The `add_trailing_spaces()` function handles this when writing to file via `--output`, but not when outputting to stdout.
- **`ingest_roll_call` expects 10 rankings** — warns but still works with 9. No `--week-end` flag; use `--week` with Monday date.
- **Duplicate user mappings** — e.g. Jones | Rarestone Compass had two TelegramUserMappings. Use `.get(id=N)` not `.get(linked_name=...)` when duplicates exist.
- **Canonical publishing runtime** — use the Mini checkout at `/Users/gerrithall/dev/ox/strongasan0x` through `scripts/roll_call_runtime.sh`. It clears project-specific shell variables so Django reads this checkout's `.env` using the shared Mini Python. The `.env` must contain Strong as an 0x X credentials and `@StrongAsAn0xBot`, not credentials from another project. Run `preflight_roll_call_syndication` after the page is live and before any social command; it verifies the bot identity and group access, required packages, X settings, and the public URL without posting. The persistent Telegram LaunchAgent intentionally invokes the interpreter and `manage.py` directly: launchd does not read the interactive shell's stale variables.
- **CurveCap Garmin attestations** — the Telegram `raw_text` is the source of record. Do not add later Garmin detail, historical weights, or a different date range by default. CurveCap intentionally reports Sunday–Saturday; that is a standing reporting convention, not a data defect. Garmin zero-days (`0` steps, kcal, and resting HR) are still a missing-sync warning. Historical Y circuit loads are only an approximate pattern, never proof of a particular set this week. In a genuine close tie, only a user-authorized CurveCap edit that favors the opponent is allowed; otherwise judge the submitted attestations as submitted.
- **`generate_twitter_rankings`** — legacy text-only path; do not use it for normal syndication. `post_rankings_to_x` and `post_rankings_to_telegram` use the ingested canonical on-site URL.

## Weekly Publishing Logs

Logs are stored in `logs/YYYY-MM-DD/` (keyed by week-end Sunday date):
- `ode.txt` — Generated Homeric ode with trailing spaces
- `substack_YYYY-MM-DD.md` — Full Substack markdown (ode + rankings table + attestations)
- `prompt.txt` — Ranking prompt sent to AI
- `trial_N_provider_model.json` — Individual ranking trial results
- `attestations_YYYY-MM-DD.txt` — Exported attestations
