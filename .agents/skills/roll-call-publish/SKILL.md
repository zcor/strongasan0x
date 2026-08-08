---
name: roll-call-publish
description: Publish the staged weekly "Strong as an 0x" Roll Call post live and syndicate it to 𝕏, Telegram, and Discord, then update Discord roles. Use only when the current user message explicitly authorizes going live for a week already staged by roll-call-prep. Do not use to prepare a week, run ranking trials, generate an ode, or re-post a week that is already published.
---

# Roll Call — publish and syndicate

Every step here is public and cannot be undone. Tweets and group messages cannot be unsent, and
flipping `is_published` makes the page visible to the world.

**Do not run any command in this skill unless the current user message explicitly authorizes
publishing.** A week being staged and ready is not authorization. If you are unsure, report what is
staged and ask.

The week must already be staged by `roll-call-prep`: `full_text` populated, rankings ingested,
`is_published` still false. Read [CLAUDE.md](../../../CLAUDE.md) for the command reference.

Run this workflow from the Mini's canonical checkout, `/Users/gerrithall/dev/ox/strongasan0x`.
Its `.env` owns the Strong as an 0x X credentials and `@StrongAsAn0xBot`; do not borrow a
different project's bot configuration or assume a desktop checkout has the same runtime. Run every
command below through the [runtime wrapper](../../../scripts/roll_call_runtime.sh), which clears inherited project credentials
before loading this checkout's `.env`.

## Date flags

`--week-end` is the **Sunday**. `--week` is the **Monday publication date** and resolves to the week
before it. The syndication commands are split across both conventions — check this table rather than
assuming:

| Command | Flag |
| --- | --- |
| `ingest_roll_call` | `--week` (Monday) — has **no** `--week-end` |
| `post_rankings_to_x` | `--week-end` (Sunday) |
| `post_rankings_to_telegram` | `--week-end` (Sunday) |
| `post_rankings_to_discord` | `--week` (Monday) |
| `assign_top_ten_role` | neither — `--weeks` is a **count** of recent weeks, not a date |

Do not use `publish_roll_call`; it is unmaintained since 2026-02-14, prompts interactively, skips
Telegram entirely, and only generates the tweet instead of posting it.

## 1. Precheck the stored URL — mandatory

Every week's row is auto-created with a **fabricated Substack URL**
(`rollcall/utils/rollcalls.py:58`, e.g. `https://strongasan0x.substack.com/p/week-of-2026-07-13`).
The 𝕏 and Telegram commands both read that field and fall back to a guessed Substack slug
(`post_rankings_to_x.py:28`, `post_rankings_to_telegram.py:33`). Publishing without checking
broadcasts a dead link to every channel at once.

```sh
scripts/roll_call_runtime.sh shell -c "
from rollcall.models import WeeklyRollCall
rc = WeeklyRollCall.objects.get(week_end_date='<week-end>')
print(rc.is_published, len(rc.full_text or ''), rc.substack_url)
"
```

Require all three: `is_published` is `False`, `full_text` is a full post rather than a stub of a few
dozen characters, and `substack_url` starts with `https://strongasan0x.com/roll-call/`.

If the URL still points at substack.com, **stop** and re-run the ingest step from `roll-call-prep`
with the correct `--substack-url`. Do not edit the field by hand and do not proceed.

## 2. Publish the page

Re-run the ingest with the publish flag:

```sh
scripts/roll_call_runtime.sh ingest_roll_call \
  --week <monday> \
  --substack-url https://strongasan0x.com/roll-call/<week-end>/ \
  --text-file logs/<week-end>/substack_<week-end>.md \
  --rankings '<json>' \
  --overwrite --publish
```

Confirm `https://strongasan0x.com/roll-call/<week-end>/` renders for a logged-out viewer before
syndicating anything. Everything below points at this URL; if the page is broken, every channel gets
a broken link.

No deploy is needed — the post is database-driven. `ox-deploy` is unrelated to publishing a week.

## 3. Verify the syndication runtime — mandatory, no-send

Run this only after the page returns 200 publicly. It checks the canonical URL, the full post,
Pillow and tweepy, X credential presence, and that `@StrongAsAn0xBot` can access the configured
Strong as an 0x supergroup. It sends nothing.

```sh
scripts/roll_call_runtime.sh preflight_roll_call_syndication --week-end <week-end>
```

If it fails, stop. Do not try a different project's `.env`, substitute another bot, or begin a
partial social rollout. Repair the Mini's `strongasan0x/.env` or its virtual environment first.

## 4. Post to 𝕏

```sh
scripts/roll_call_runtime.sh post_rankings_to_x --week-end <week-end> --dry-run
scripts/roll_call_runtime.sh post_rankings_to_x --week-end <week-end>
```

Dry-run first, every time. Tweet 1 carries the rankings plus the rendered winner-stanza image, then
a 60-second sleep, then tweet 2 replies with the recruitment and links block.

Tweet 2 **historically** 403'd on the free API tier, and the command has a trimmed fallback for it.
That is no longer reliable: on 2026-07-23 the full tweet 2 posted successfully. So do not report a
403 as "expected and fine" without looking — check the output and say what actually happened. Either
way tweet 1 stays live; capture its URL.

`generate_twitter_rankings --week <monday> --post` is the older text-only path, untouched since
2026-04-27. Use it only when the user wants to hand-edit the tweet text before posting.

## 5. Post to Telegram

```sh
scripts/roll_call_runtime.sh post_rankings_to_telegram --week-end <week-end> --dry-run
scripts/roll_call_runtime.sh post_rankings_to_telegram --week-end <week-end>
```

Defaults to the "Strong as an 0x" supergroup (`-1003122619283`). It sends **two** messages: a
`sendPhoto` carrying the winner stanza with rankings and the link in its caption, then a follow-up
`sendMessage` with the stats table. Expect two message IDs in the output and report both — the
command's own module docstring still claims a single photo, which is stale.

## 6. Post to Discord

```sh
scripts/roll_call_runtime.sh post_rankings_to_discord --week <monday>
```

Note this one takes `--week`, the Monday. It posts to `#🎖️︲rankings`.

## 7. Update Discord roles

```sh
scripts/roll_call_runtime.sh assign_top_ten_role --dry-run
scripts/roll_call_runtime.sh assign_top_ten_role
```

## 8. Report

Give the user the live post URL, the tweet 1 URL, and confirmation of the Telegram and Discord
posts. State the actual outcome of tweet 2; do not call a 403 expected or harmless without current
evidence.
