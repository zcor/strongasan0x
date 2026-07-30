# CLAUDE.md — strongasan0x

## Project Overview

Django-based weekly fitness contest platform ("Strong as an 0x"). Warriors submit weekly health attestations via Discord and Telegram bots, AI models rank them, and results are published to Substack with a Homeric ode.

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

# Weekly publishing (see WEEKLY_PROCESS.md in garmin_project for full checklist)
python manage.py list_attestations
python manage.py run_ranking_trial --week-end YYYY-MM-DD --provider deepseek
python manage.py run_ranking_trial --week-end YYYY-MM-DD --output-only
python manage.py generate_substack_ode --week-end YYYY-MM-DD --provider deepseek --output ode.txt
python manage.py ingest_roll_call --week YYYY-MM-DD --substack-url URL --rankings 'JSON' --publish --overwrite
python manage.py post_rankings_to_discord --week YYYY-MM-DD
python manage.py generate_twitter_rankings --week YYYY-MM-DD
python manage.py assign_top_ten_role
python manage.py extract_metrics  # only processes is_published=True weeks

# Full workflow (orchestrates all steps)
python manage.py publish_roll_call --week-end YYYY-MM-DD --substack-url URL

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
- **CurveCap Garmin attestations** — auto-generated from watch data, don't include weight amounts. Must manually add based on historical patterns: Mon/Wed/Sat circuit class (66-110 lb), Tue/Thu/Fri/Sun heavy lifting (bench 230-260 lb, back/legs 200 lb).
- **`generate_twitter_rankings`** — date formatting bug shows "Feb. 23 - 1, 2026" instead of "Feb. 23 - Mar. 1, 2026". Substack slug is auto-generated from `--week` date, not the actual Substack URL slug.

## Weekly Publishing Logs

Logs are stored in `logs/YYYY-MM-DD/` (keyed by week-end Sunday date):
- `ode.txt` — Generated Homeric ode with trailing spaces
- `substack_YYYY-MM-DD.md` — Full Substack markdown (ode + rankings table + attestations)
- `prompt.txt` — Ranking prompt sent to AI
- `trial_N_provider_model.json` — Individual ranking trial results
- `attestations_YYYY-MM-DD.txt` — Exported attestations
