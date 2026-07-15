# Daily app — handoff (2026-07-14)

Portable context for continuing `daily/` work in **`zcor/strongasan0x`**. Paste this
to Claude at the start of the new-repo session, or have Claude copy the memory files
from daily-climb's memory dir into the new project's memory dir.

## Status
- Full-coach beta is **built, ported to `zcor/strongasan0x` (PR #48), deployed, and LIVE on prod.**
- `zcor/daily-climb` is **RETIRED** — a staging copy. All future `daily/` work happens in `strongasan0x`.

## Repo & deploy (do not deviate)
- Work in **`zcor/strongasan0x`**: branch → PR → merge to `main`; **zcor deploys**.
- Prod auto-deploys via wrapper `ssh leviathan-api "sudo -n /opt/ox/deploy/ox-deploy"`
  (hard-resets tree to `main` + restarts Apache). **Never** manual `git pull`/`migrate`/`touch wsgi.py`
  on the server — it gets discarded. Old DEPLOYMENT.md manual flow was stale (fixed).
- `claude` SSH user (`ssh leviathan-api`) is read/inspect-only: no sudo, can't read app `.env`,
  can't write `wsgi.py`. `wsgi.py` mtime is NOT a restart signal (wrapper doesn't touch it).

## What shipped this last (full-coach) round
- Onboarding purpose question "What's this mostly for?" → **Health & fitness** sets
  `ai_mutations_enabled=True` (full coach: overnight notes, Plan tomorrow, Wrap up);
  **Life in general** = support-only cheerleader. Copy states the consequence.
- Overnight prompt **health/fitness scope gate**: coach only edits a list when every core item
  is health/fitness; any out-of-domain item → byte-identical list, no bonuses, plain note.
  Explicit user requests still honored.
- **Stale-proposal reconcile guard** (migration `0016` `CoachSuggestion.base_questions`):
  a proposal generated last night no longer reverts instant edits made after it. Beta reconciles
  adds/removes at apply time; legacy applies verbatim.
- **"Wrap up my day" chip** restored in beta chat (gated on `ai_mutations_enabled`).
- User-facing rename **"The Climb" → "Daily"**; onboarding rebuilt in the Daily design language.
- Accessibility: meaningful text off `--text-faint` (fails WCAG AA) → `--text-dim`.
- Earlier rounds (already live): wins facet (north stars + stepping stones + Achieved page),
  nested habit sub-items, metrics ("Today's numbers"), and ~16 reviewed bug fixes.

## Standing rules (in memory; keep applying)
- App name is **"Daily"**, never "The Climb" (that's the internal codename only).
- **No em-dashes** in user-facing copy (commas/colons/separate sentences).
- Meaningful text uses **`--text-dim` minimum** (WCAG AA); `--text-faint` = decorative glyphs only.
- **Never read the project `.env`** (secrets).

## Product context
- **Spencer** = the one validated daily user (health habits + weight/grip/pain metrics). The
  loop must not break for him; full-coach beta was verified against a fabricated persona mirror
  (not his real data — prod is unreachable from local). Two flags to migrate a user to full coach:
  `beta=True`, `ai_mutations_enabled=True` (+ `focus="health"` so chat coaching is tuned).
- **Modes:** support-only (mutations off, cheerleader) vs full coach (mutations on). Gated on
  `ai_mutations_enabled`.
- The **wins facet is still unproven** (n=0); lead with repetition+metrics.

## Open follow-ups (not yet done)
- **Metrics discoverability**: "Today's numbers" renders at the very bottom of the beta page,
  below the wins card — a UX regression for metrics-first users (Spencer). Consider reordering.
- **No UI to add a metric** (both legacy & beta) — metrics only exist via backfill/shell. The
  design doc's "coach-offered activation" was never built. Highest-value Spencer investment.
- **No trend charts** (metrics Phase 2 never built).
- **No Undo** for an applied overnight mutation in either template (server supports it).
- The user's own account is currently on the **support-only** variant, not full coach — confirm
  which they want.
- Verify the 39 daily tests exist/pass in strongasan0x.
