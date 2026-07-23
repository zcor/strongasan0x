---
name: roll-call-prep
description: Prepare the weekly "Strong as an 0x" Roll Call post — ranking trials, the Homeric ode, the post markdown, and a staged unpublished ingest. Use when the user says it is a new week, asks to finalize or run the roll call, run ranking trials, or generate the weekly ode. Ends at a staff-only preview link; never publishes and never posts to 𝕏, Telegram, or Discord — roll-call-publish does that.
---

# Roll Call — prepare the week

Run every command from the repository root with the project's Python. Read
[CLAUDE.md](../../../CLAUDE.md) for the full command reference and known gotchas; this skill
supplies the order, the judgment calls, and the stopping point.

Posts are **self-hosted** at `https://strongasan0x.com/roll-call/<week-end>/`. Substack is retired.
Nothing you do in this skill is visible to anyone but staff: unpublished weeks 404 for everyone
else, and the ingest step is idempotent under `--overwrite`. Stop before publishing.

## Date flags — get this right first

- `--week-end` takes the **Sunday** that ends the week.
- `--week` takes the **Monday publication date** and resolves to the week before it.

Mixing these up silently targets the wrong week. Fix the week in your head once, write both dates
down, and reuse them.

## Do not use `publish_roll_call`

`rollcall/management/commands/publish_roll_call.py` is unmaintained since the 2026-02-14 initial
release. It predates the ode command and every syndication command, uses interactive `input()`
prompts that raise `EOFError` in a non-interactive shell, never posts to Telegram, and only
generates the tweet rather than posting it. Ignore its advertisement in `CLAUDE.md`. Run the steps
below individually.

## 1. Review attestations

`list_attestations` takes `--days`/`--source`/`--limit` and has **no week filter**, so scope the
week yourself:

```sh
python manage.py shell -c "
from rollcall.models import Attestation, WeeklyRollCall
rc = WeeklyRollCall.objects.get(week_end_date='<week-end>')
for a in Attestation.objects.filter(weekly_roll_call=rc).order_by('posted_at'):
    n = a.telegram_user.linked_name if a.telegram_user else '?'
    print(n, a.posted_at, a.source, 'hidden=', a.is_hidden, len(a.raw_text or ''))
"
```

The name lives on `telegram_user.linked_name`; `Attestation` has no `display_name` field.

Attestations for a week normally arrive on the **Monday and Tuesday after it closes**, and the next
window does not open until Friday (`ATTESTATION_WEEKEND_START_HOUR`). An empty current week midweek
is expected and is *not* evidence of a stuck bot — judge bot health by whether last week's
attestations landed, not by silence today.

Hide spam or duplicates through `/review-attestations/`. Add anything the bot missed:

```sh
python manage.py import_attestation --name "<warrior>" --text "<their text>" --week-end <week-end>
```

For a manual entry always pass `posted_at`, `source='telegram'`, and `telegram_user`, but leave
`telegram_message_id` unset so the bot can still insert the canonical copy later. Late attestations
belonging to the prior unpublished week are reassigned automatically in code.

If the newest attestation is days old the bot may be stuck. Telegram only retains undelivered
updates for about 24 hours, so a multi-day outage means those attestations are gone and must be
pasted in by hand.

CurveCap's Garmin-generated attestations carry no weights. Fill them in by editing `raw_text` in
place — never create a second attestation. Mon/Wed/Sat is circuit class at 66–110 lb; Tue/Thu/Fri/Sun
is heavy lifting (bench 230–260, rows/squats/deadlift around 200).

## 2. Run ranking trials

Use DeepSeek. Anthropic credits are frequently depleted, and `--provider deepseek` costs roughly
0.0004 USD per trial — a four-trial week is well under a cent.

```sh
for i in 1 2 3; do
  echo "n" | python manage.py run_ranking_trial --week-end <week-end> --provider deepseek
done
```

The piped `echo "n"` answers the interactive "Run another trial?" prompt, which otherwise raises
`EOFError`.

**Never pass `--auto-continue`.** When warrior names fragment it can run well past a hundred trials.
Start with 3–4. Add more only if the table below is genuinely unseparated; 8–15 is normal for a
contested week.

## 3. Read the table, not the verdict

```sh
python manage.py run_ranking_trial --week-end <week-end> --output-only
```

Always paste the **full Avg/StdErr table** into the conversation. "Converged: False" is a
conservative 2σ-overlap test and routinely hides real separation — if two averages differ by more
than roughly 3× the larger standard error, that is a real gap. Present the numbers and let the user
resolve genuine ties; ranking order is their editorial call, not yours.

## 4. Export the ranked attestations

```sh
python manage.py run_ranking_trial --week-end <week-end> --output-only \
  --output-ranked-attestations logs/<week-end>/attestations.txt
```

## 5. Generate the ode

```sh
python manage.py generate_substack_ode --week-end <week-end> --provider deepseek \
  --output logs/<week-end>/ode.md
```

Write the `.md` file so the user can preview and copy it.

**Proofread every number against the source attestation before going further.** DeepSeek
hallucinates weights and rep counts, flips kg and lb, confuses one warrior's lifts for another's,
and mistypes names. This is the single most common defect in the weekly post.

Also check:

- Naly is male — he/him.
- Every verse line ends with two trailing spaces, or the line breaks collapse. Re-add them after
  any manual edit; `--output` adds them on write, stdout does not.
- Swap tied ranks to match whatever the user decided in step 3.

## 6. Build the post markdown

Assemble `logs/<week-end>/substack_<week-end>.md` from the ode, the rankings table, and the full
attestations.

The page is rendered by a deliberately small renderer — see
[roll_call_markdown.py](../../../rollcall/services/roll_call_markdown.py). Stay inside its supported
subset: `#`/`##`/`###` headings, `**bold**`, `*italic*`, `---` rules, `- ` bullets with `  - `
nesting, `|` tables, standard inline links, backslash-escaped punctuation, and hard line breaks from
trailing double spaces. Anything else will not render. Escape `|` inside warrior names or use `/`.

## 7. Ingest, staged and unpublished

```sh
python manage.py ingest_roll_call \
  --week <monday> \
  --substack-url https://strongasan0x.com/roll-call/<week-end>/ \
  --text-file logs/<week-end>/substack_<week-end>.md \
  --rankings '<json>' \
  --overwrite
```

**`--substack-url` is mandatory even though nothing here touches Substack.** The field is legacy and
now stores the canonical on-site URL. Each week's row is auto-created with a *fabricated* Substack
link (`rollcall/utils/rollcalls.py:58`), and the syndication commands read this field — so leaving
it alone means broadcasting a dead link later.

Rankings must be sequential with no duplicate ranks; `ingest_roll_call` rejects duplicates. Use
editorial tie-breaks rather than repeating a rank. It expects 10 entries and warns but still works
with 9.

**Do not pass `--publish`.**

## 8. Hand back the preview and stop

Report the preview URL — `https://strongasan0x.com/roll-call/<week-end>/` — and note that it is
staff-visible only. Summarize the final ranking order and anything you corrected in the ode.

Then stop. Publishing and syndication are a separate, explicitly authorized step. If the user wants
to go live, that is `roll-call-publish`.
