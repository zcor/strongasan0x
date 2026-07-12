# The Climb: Product Reframe Plan

A planning spec for review. Captures the target product direction, the rationale
behind each decision, what already exists in the codebase, what changes, and the
questions still open. Written for a reviewer to pressure-test whether the plan is
solid.

Status legend: **[DECIDED]** settled in planning. **[OPEN]** still needs a call.
**[EXISTS]** already in the codebase. **[NEW]** to be built. **[GATE]** kept but
put behind a flag. **[CUT]** removed or demoted.

Note on style: user-facing copy in this app must never use em-dashes (use commas,
colons, or separate sentences). This doc follows that rule too.

---

## 1. Vision

The Climb is a dead-simple daily checklist. The user picks a few small things that
matter and checks them off each day. Two kinds of "things that matter":

1. **Habits they want to keep** (recurring behaviors: exercise, water, sleep).
2. **Wins they want to claim** (meaningful things they keep putting off).

These are NOT rival modes to A/B test. They are the two facets of ONE product. People
fail to do what matters in exactly two ways: they let good recurring habits slip, and
they avoid the meaningful things while drowning in busywork. The Climb handles both in
one loop: a checklist for the habits you keep, and a way to surface and knock down, one
at a time, the things you tuck away from doing. Together they cover both failure modes.

Honest caveat on evidence (see section 1a): habits plus metric-tracking is validated by
our one daily user; the wins facet is integral to the vision but not yet proven to work
(the test is completion, not hoarding). So we lead with the validated habit core and
prove the wins facet works, rather than treating the two as a competition.

Guiding principles:
- **User-authoritative.** The user curates their own list, instantly. The AI never
  silently rewrites it (by default, see section 3).
- **Low friction.** Start with one item, never a wall of them.
- **AI optional.** The AI (Jamie) is a helper the user summons, not a voice always
  talking or a system that takes control.

---

## 1a. Positioning, audiences, and honest validation status

**Positioning decision: co-equal expansion, framed as ONE product with an optional
power mode (not two products).** **[DECIDED]** The app serves both a health/fitness
audience and a general audience. The failure mode is "sometimes a fitness app,
sometimes a productivity app," which rots the identity. The version we commit to:

> The Climb is a personal daily tracker. You curate small things that matter, habits
> you want to keep and things you keep putting off, and check them off. If you want,
> Jamie can also auto-curate and adapt your list for you.

In that frame, the existing fitness "warriors" are not a separate product; they are
**power users who turned on AI curation** (`ai_mutations_enabled = True`). General users
are the same product with that mode off. One identity, one codebase, one product; the
fitness-coach engine is an optional layer, not a second app.

**Validation status (this is the least-solid part of the plan, face it head on).** The
one validated daily user ("Spencer," a named persona the metrics feature was built
around, so real and roughly arm's length, not founder-as-user) lives **100% in
Repetition mode**: his list is Slept 8+ hrs, Stretch 15 min, Walk 20 min, plus
bodyweight logging. So:
- **Validated:** repetition (recurring health habits) plus metric-tracking.
- **Unproven (n=0):** the wins facet. Integral to the one-product vision, but no user has
  yet shown it works (completing put-off things, not hoarding them).
- Implication: do NOT stake the identity/marketing on wins yet. Lead with the validated
  repetition-plus-metrics experience; the wins facet is the part still to prove out.
- **Metrics is a validated PILLAR, not an afterthought.** Spencer's daily loop includes
  bodyweight logging (`DailyMetric`/`DailyMetricReading`). Treat metric-tracking as part
  of the core to preserve and surface, not merely "reused" plumbing (see section 8).
- **Protect the one validated user.** No migration, gating change, or engine sunset ships
  without verifying against Spencer's real account that it does not disrupt his daily
  flow. He is the only proof the loop works; losing him to a careless change would be
  self-inflicted.
- **[OPEN, needed]** Is Spencer on `ai_mutations_enabled` (is the engine earning its keep
  with our best user, making sunset riskier), and is he a bridged warrior or a general
  external user (which audience does our only validation belong to)?

**Beachhead question (still open, matters for go-to-market).** "Everyone who
procrastinates" is not launchable. The wedge is still open, with general self-use as the
broader vision. **[OPEN]** Also unresolved: existing users came via the rollcall contest
and bots; a general audience needs a new acquisition channel that does not exist yet.
**[OPEN]**

**Two independent axes (do not conflate them).**
- **Focus / domain: health vs life.** A soft, changeable setting captured once in
  onboarding. It tunes what Jamie suggests and what she coaches (see section 3). It is
  the mechanism that makes co-equal expansion a single coherent product: the user
  declares their focus and the app adapts, so each person gets a clean single-focus
  experience even though the product serves both.
- **Mode: habit (repetition) vs win (put-off).** Available in BOTH focuses. Health has
  put-off wins too (book the dentist, get bloodwork, start PT, get to the gym you have
  been avoiding). So focus does not determine mode.

**Mode is a property of the user's RELATIONSHIP to a task, not the task's category.**
Weight lifting is a habit for a consistent lifter and a win for someone avoiding the
gym. Same task, different mode. So never pre-categorize tasks as habit-type or win-type.

**Wins graduate into habits.** A put-off thing you start doing repeatedly moves from
"today's win" to the habit checklist. This progression (from "the thing I dread" to
"the thing I do") is arguably the whole point of the app. Jamie should celebrate the
promotion, and there must be a way (user or Jamie) to promote a win to a habit once it
sticks. **[NEW: a promote-win-to-habit action]**

---

## 2. The two modes

These are **two moments in one daily rhythm**, not rival apps:
- **Repetition** happens during the day (check habits off as you go).
- **Wins** is seeded the night before or picked in the moment (name the one thing).

Every user gets both, because both are integral facets of one product, not competing
experiments. Instrumentation (section 9) tells us how each facet is used so we can
improve each, not which to keep. The two must also read as ONE coherent daily screen,
not two bolted-on lists (designed in section 2c).

### 2a. Repetition mode (the "checklist")
- A small, curated list of recurring habits, **all visible at once**.
- Cap ~20 items (`MAX_CHECKLIST_SIZE`) **[EXISTS]**. Generous ceiling, not a target.
- The user adds, swaps, and removes items themselves, instantly **[EXISTS]**.
- **Recurrence, not frequency.** A habit is not necessarily daily (walk today, run
  tomorrow). Copy must not promise "daily." Broaden item wording ("Moved my body")
  rather than build per-item scheduling. **[DECIDED]**
- **Streaks are per-DAY** (kept your list), not per-item (breaks on non-daily items).
  **[DECIDED]**
- No per-item cadence/scheduling in v1; add later only if users ask. **[DECIDED]**
- **"Already did it" on add:** when a user adds an item late in the day for something
  they already did, let them mark it done immediately. Recommended: a toggle in the
  add composer ("I already did this today") so the item lands already checked. Keep
  it a local tap, not a Jamie chat round-trip. **[OPEN: composer toggle vs Jamie-voiced
  inline nudge]**

### 2b. Wins mode (the "backlog"), framed positively
This is the reframed former "avoidance" mode. Key insight: a put-off task and a win
are the same task from opposite ends (dread before, win after). We present it as
**wins** but keep an anti-avoidance engine underneath. Pure "wins" framing alone goes
soft and reopens the trap (people claim easy wins and dodge the one that matters).
**[DECIDED: the synthesis]**

- **It is a backlog, not a checklist.** The user can add many items (up to ~100;
  could be higher since never fully rendered) **[OPEN: exact cap]**. Different storage
  and display rules from the repetition checklist. **[NEW storage]**
- **Never show all of them, or even the COUNT.** Seeing 100 is the overwhelm that caused
  the avoidance, and an "N more" count is a milder version of the same pressure (the user
  flagged "N more on your list" as stressful). The daily surface shows **one** item; the
  pile lives behind a "your list ›" door that never advertises its size. **[DECIDED]**
- **Doorway question is gentle:** "What would make today a win?" (not "what are you
  avoiding?"). The honest answer is usually the put-off thing, minus the guilt.
- **Daily surface:** header "Today's win"; one focus item; buttons "Did it" and
  "Not today."
- **Peek:** after the user taps "Did it," reveal an optional "up next" peek as a
  momentum reward, not an always-on list. Resting state stays strictly one item, so
  there is nothing to feel behind on. This mirrors the existing bonus-reveal pattern
  (`bonus_revealed` after core done) **[EXISTS pattern]**. **[OPEN: always-on peek vs
  reveal-after-win; recommendation is reveal-after-win]**

Jamie's two jobs in wins mode (the second is the real value):
1. **Pick which backlog item to surface** (one at a time). Recommended: user-ordered,
   Jamie suggests but does not impose, swap cycles to the next. **[OPEN: user-ordered
   vs Jamie-picks-highest-leverage vs oldest-first]**
2. **Scope it down.** Put-off tasks are avoided because they are big and vague ("Find
   a job"). Jamie turns today's boulder into one concrete finishable-today win ("Apply
   to 1 job from your saved list"), shown as "part of: Find a job." For v1 Jamie
   reshapes the surfaced item; the goal stays in the pile until marked done. Proper
   goal-with-substeps is a later enhancement.

**Helping a user IDENTIFY a win when they are stuck (elicit, do NOT fabricate).** One of
Jamie's highest-value moments: surfacing the avoided thing is the hard part of beating
avoidance. But unlike a habit, she cannot know the user's meaningful put-off thing, and
inventing one (suggesting "apply to a job" to someone who is not job-hunting) is noise
that breaks trust (the no-invention rule). So here she is a MIRROR, not an oracle. Three
moves: (1) reflective questions ("what has been nagging at the back of your mind?", "if
you woke up with one thing off your plate, what would it be?"); (2) example buckets to
prime the pump without guessing their specific win ("a call you're dreading, an errand you
keep skipping, a health thing you've put off, the first step of something bigger, any of
those ring a bell?"); (3) grounded reflection when she has history ("you have mentioned
your knee a few times, is dealing with that a win you have been putting off?"). Once the
user names it, she scopes it (job 2). Guardrails: no fabrication (the user names and
confirms the real thing); no manufactured pressure (a habits-only day is complete, so
this is ON-DEMAND via a "Help me find one" affordance, never pushed); tone is cheerleader,
not therapist or interrogator, and if nothing surfaces in a message or two she lets it go.
Same underlying skill as the onboarding "suggest one" branch, pointed at wins; lives on
the wins surface (Phase 2), not in onboarding.

Mechanics:
- **"Not today" = defer, not delete.** Send it back to the pile, surface a different
  one. Never punish.
- **The trap:** infinite swapping is avoidance of the avoidance. Guard: after an item
  is deferred a few times, Jamie gently offers to scope it smaller ("want me to shrink
  it to just the first paragraph?") instead of letting it vanish. Also, when a user
  keeps claiming small wins and circling the big one, Jamie gently probes ("nice win,
  anything bigger you have been meaning to get to?").
- **Retires "frog."** "Today's win" replaces "the one thing you keep putting off." The
  old eat-the-frog jargon is confusing and gets swept from all copy and AI prompts.

---

## 2c. The combined daily screen (the "one product" test)

This is the single most-seen screen and the thing that makes "one product" true or
false. The trap: two walled-off boxes that read as two apps sharing a login. The fix:
**one scrolling screen, shared card language, the win as the elevated "crown" on top and
habits as the body.** **[DECIDED]**

Principles:
- **One list, one visual language.** Same card style and check affordance everywhere, so
  it reads as a single daily list, not two.
- **Win on top, and it is optional. [DECIDED]** When there is a win today it sits at the
  top, starred and prominent. Rationale: the win is the highest-ROI thing the user can do
  that day (frog-first), so it is the headline while they are fresh. When there is no win,
  it shrinks to a slim, dismissible footer invitation, never a big empty card that nags.
- **Habits are the floor**, below the win. Metrics and bonus come after.
- **The win is a crown in the one list, not a separate zone. [DECIDED]** (coherence)
- **Either zone can be empty.** The screen must degrade gracefully to habits-only
  (Spencer) and win-only (a life-focus user with no habits yet).
- **Habit-only users:** the win invitation is dismissible and remembered. [DECIDED] Tap it
  away once and it stays gone; no settings trip required.

**Visual direction (settled during in-session design iteration, 2026-07-09):**
- **Greeting header** "Hi, [name]." in the gold gradient, date beneath (NOT a "The Climb"
  brand line).
- **Week strip** of 7 small ring-circles (last 7 days, day letters beneath), each filling
  green with that day's completion, so check-in CONSISTENCY reads at a glance (the user
  values this). Today's circle fills live as items are checked, so the strip also carries
  today's progress. This REPLACED a separate top progress ring (removed as redundant).
- **Squared cards** (no rounded corners), hairline border, with a DIVIDED FOOTER of
  actions (content on top, hairline, actions below): the win card footer holds "Did it" /
  "Not today"; the habits card footer holds "Add habit".
- **Palette kept:** navy `#1a1a2e -> #16213e`; gold `#ffd97a` = accent/brand/win; green
  `#78c878`/`#8fe08f` = progress/done. Round check circles (green when done, gold on win).
- **Thin type** (light weights, win title ~300), system sans, no serif, no emoji (line
  SVG icons only).
- **Two FABs bottom-right:** the coach speech-bubble (gold) with a smaller "?" help button
  above it.
- **Metrics are opt-in and understated:** a metric-tracker (Spencer) sees ONE quiet line,
  NOT a boxed "Numbers" section; absent entirely for everyone else.
- **Never show the backlog's size** (no "N more" count); offer only "Your list" access.

Layout sketch (the Visual direction above is the settled look):

State: both (win + habits), win not done:
```
The Climb              [?] [💬]
★ TODAY'S WIN
  [ ☐ Apply to 1 job ]
     part of: Find a job
  [ ✓ Did it ]   [ Not today ]
DAILY HABITS            1 / 3
  [ ✓ Slept 8+ hrs ]
  [ ☐ Stretch 15 min ]
  [ ☐ Walk 20 min ]
  + Add habit
```

State: habits only (Spencer) -> win becomes a slim dismissible footer, not a top box:
```
DAILY HABITS            1 / 3
  [ ✓ Slept 8+ hrs ] [ ☐ Stretch 15 min ] [ ☐ Walk 20 min ]
  + Add habit
  · Got something you keep putting off? Make it today's win ›
```

State: win only (no habits yet) -> win crown plus an optional "add a daily habit" nudge.

State: after "Did it" -> win collapses to a done row and reveals the peek ("On a roll?
Up next: ...  [ I'm good today ]"), habits stay put. Mirrors the existing bonus-reveal.

---

## 3. Jamie (the AI): narrowed, with the old engine gated

**By default, Jamie does only two things and never edits the list:**
1. Moral support (grounded encouragement). Framed to the user as "I'm Jamie, here to
   cheer you on (and help when you're stuck)."
2. Unblock a stuck user who needs help naming an item.

**Jamie's guidance DEPTH shifts with FOCUS; her role and her editing power do not.**
She is ONE consistent supporter everywhere: "here to cheer you on (and help when you're
stuck)." She is not a "coach" in the directive, take-the-wheel sense. What changes with
focus is only how substantive her guidance can be, not her job. **[DECIDED, option A]**
- **How much she guides (set softly by focus):** **Health focus:** she can lean on real,
  safe, general health knowledge (sleep, recovery, hydration, mobility, protein), so her
  encouragement can be specific ("you're walking daily but never logging sleep, that
  might be the gap"). **Life focus:** she stays out of the substance of your work/life
  (AI advice on how to do your job reads as preachy, per the existing "app's lane" rule);
  her help is process-only (scoping the put-off boulder into a today-sized win) plus
  encouragement. Same supporter, deeper vs lighter guidance. A cheap, safe prompt/tone
  difference, not a second persona.
- **Her power to edit the list (a SEPARATE opt-in):** whether Jamie can actually rewrite
  the list is `ai_mutations_enabled`, off by default, and it is NOT tied to focus. A
  health user gets a more knowledgeable Jamie who advises and encourages, but she still
  does not rewrite their list unless they turn that on. DANGER to avoid: do not let
  "picked health" silently enable the mutation engine, that would re-expand the exact
  risky behavior we are trying to shrink and sunset.

This deliberately removes the scariest behavior the app currently has: the AI silently
rewriting the user's list overnight. Most of the current `SYSTEM_PROMPT` complexity
(progression, anti-ratchet, "keep the same number of items," count enforcement) exists
only to make that safe. Dropping it removes a large surface of trust risk.

**But we do not delete it. We gate it (backward compatibility).** **[DECIDED]**
- Add `ai_mutations_enabled` to `DailyParticipant`, default `False`. **[NEW]**
- New users: `False` (support-only Jamie, one-card onboarding).
- Existing users: with the `beta` parallel-run rollout (section 12), NO grandfather
  migration is needed, existing users stay entirely on the current UI/code path (which
  already runs the mutation engine as today). `ai_mutations_enabled` is only meaningful
  WITHIN the new (beta) experience, where it defaults off. (Superseded the earlier "set
  all existing users True" migration.)
- User-facing toggle: "Let Jamie adjust my checklist overnight."
- Kept behind the flag, dormant for opted-out users **[GATE]**: `generate_suggestion`
  list-rewriting, progression/anti-ratchet, `_parse_response` count enforcement,
  `apply_pending_mutations`, auto bonus/swap/stretch (`generate_one_bonus`,
  `CORE_SWAP_PROMPT`, `STRETCH_PROMPT`), profile distillation (`distill_profile`). The
  overnight note stays for everyone.
- **Sunset strategy:** remove the engine once data shows it is unused. Must instrument
  the trigger or cleanup never happens. Track how many active users have the flag ON,
  and (the truer signal) how many have actually accepted an AI mutation recently vs
  dismissed suggestions / no `AI_MUTATION` `ChecklistVersion` in ~30 days. Concrete
  rule: remove when fewer than N active users have accepted a mutation in the last 30
  days. Mark the gated code `# DEPRECATED ... sunset when usage -> 0` and log one line
  each time a mutation actually fires.

**Consequence to accept honestly:** the simplification is "off by default," not "gone."
You still maintain the big `SYSTEM_PROMPT` until sunset.

**Chat prompt must branch on the flag.** `CHAT_SYSTEM_PROMPT` currently tells Jamie
"you cannot edit the list, changes come tomorrow." That promise is true only for
opted-in users. For a support-only user it is a lie (no overnight change is coming);
her honest answer is "tap + Add item, it is instant." So the chat prompt (and the
evening planning behavior) needs two variants keyed off `ai_mutations_enabled`. **[NEW]**

---

## 3a. Jamie guardrails and safety [NEW, required before broad launch]

The existing prompts have only fitness-domain and correctness guardrails (no-invention,
"app's lane," honesty about list changes). Those were written for a fitness coach. The
moment the app went general (wins about anything, open chat, bad habits), new risk
surfaces opened that the current prompts do NOT cover. The user
chose to keep the habit list OPEN (any habit allowed), so scope-narrowing does not close
these; guardrails must.

**Where the risk actually enters:** not the habits facet, but wins-anything and the open
chat. Those guarantee heavy content eventually gets typed.

**Solution: a single SHARED safety preamble injected into EVERY Jamie prompt** (overnight
note, live chat, planning, win scoping, win-identification, bad-habit support), so the
rules hold in every mode. Rules:
- **Scope discipline.** She is an encouraging daily supporter, not a general assistant,
  therapist, doctor, lawyer, or financial advisor. Help with the checklist, scoping tasks,
  and encouragement. (Her substantive *advice* stays health-only; process-plus-support for
  everything else, per section 3. This bounds out-of-lane advice but is ORTHOGONAL to the
  crisis rule below.)
- **Crisis and clinical (highest priority).** If a user signals self-harm, suicidal
  thoughts, abuse, an eating disorder, or serious addiction, do NOT try to treat it and do
  NOT brush it off. Respond with brief care and point to appropriate real help / crisis
  resources. Staying in her lane on advice is NOT the same as handling a crisis well.
- **Defer to professionals.** On medical, legal, financial, or mental-health questions,
  give no authoritative advice; encourage a qualified person.
- **No invention** (extend the existing rule): never state facts, numbers, or claims she
  does not have.
- **Non-judgment.** Never shame, especially around slips or avoidance. Compassion, not
  lectures.
- **No manufactured pressure / dark patterns.**
- **Stay on task.** Decline off-topic or inappropriate requests warmly and redirect; she
  is not a general-purpose chatbot.

**Implementation notes and honest limits:**
- Prompts REDUCE risk, they do not eliminate it; LLMs still slip.
- For the crisis case specifically, do NOT rely on the model alone. A lightweight
  keyword/pattern detector that triggers a vetted, human-written response (with real
  resources, e.g. a crisis line) is far safer than hoping the model handles it every time.
- A general-life app that can brush against mental health carries real responsibility and
  some liability. Consider a professional review before opening it broadly. (Not legal
  advice, a flag.)
- **Phasing:** the shared safety preamble and crisis handling are required in Phase 1, as
  soon as any general user can chat with Jamie, not deferred to a later phase.

---

## 4. Onboarding flow

Start with **one card, not a seeded list**. Today new users get three baseline items
auto-seeded (`BASELINE_QUESTIONS` in `get_or_create_current_checklist`) **[EXISTS]**;
change this so new users land near-empty. **[NEW]**

Screen 1 (purpose + fork):
```
I'm Jamie, here to cheer you on (and help when you're stuck).

The Climb keeps it simple. Pick a few small things that matter and check them
off each day: the habits you want to keep, and the things you keep putting off.

Let's start with just ONE.

[ Suggest one for me ]   -> Branch A
[ I've got one ]         -> Branch B
```

Branch A ("Suggest one") runs **one** light question, and it asks FOCUS, not mode:
```
What should I help you with first?
[ 🌱 My health ]   [ 🎯 My life ]
```
Then Jamie proposes a single starter **habit** in that world (accept / reword / try
another). Why this question and not "habit vs win":
- It is the input Jamie needs to make a good cold-start suggestion (a new external user
  has no data), so it earns its place instead of being a generic survey.
- It sets the health-vs-life focus (soft, changeable in settings) that makes co-equal
  expansion one coherent product.
- Mode is NOT asked. Both surfaces (habit checklist and "today's win") are available to
  everyone afterward, scoped to their focus. First suggestion is a habit on purpose:
  it is the gentler entry and the mode Spencer validates. Wins is introduced as a
  discoverable surface once they are in ("want to also knock out something you have been
  putting off?"), not forced on a brand-new user.
Cap it at this one question; never a survey. Focus is a soft default (tunes Jamie's
suggestions and persona), never a hard lock on what the user may add.

Branch B ("I've got one") opens an input field. After they save, Jamie drops one warm
line ("Nice, that's your first one. Add more whenever, or just start here."), then gets
out of the way.

Both branches land on the real check-in screen with a single card plus "+ Add item."

---

## 5. Existing "How this works" intro (already changed)

The inline "How this works" card was moved into a "?" help FAB stacked above the coach
chat button, opening a modal. The modal auto-opens once for new users / on intro-version
bump and is reachable anytime via "?". Copy was updated to the user-curated framing and
de-em-dashed. **[DONE in `daily/templates/daily/checkin.html`]**

---

## 6. Managed / sponsored lists (delegation layer) [CUT]

**[CUT 2026-07-09]** Assigning / building a list for someone else (a parent sponsoring an
adult child's list, coach/client, accountability buddy) is removed from the product. The
Climb is a single-user personal tracker: every account is self-owned and self-curated.
No delegation layer, no manager/doer roles, no share codes, no cross-user access. This
eliminates the plan's only multi-user attack surface (see section 8a) and drops former
Phase 3 entirely (section 12).

---

## 7. Copy and tone rules

- **No em-dashes** in any user-facing copy. Use commas, colons, or separate sentences.
  Also applies to Jamie's generated output (add a "no em-dashes" line to the system
  prompts when doing the prompt work). **[DECIDED]**
- **Sweep out "frog"** everywhere: `daily/services/ai_coach.py`, `daily/views.py`,
  `daily/services/onboarding.py`, `daily/templates/daily/checkin.html`,
  `daily/models.py`, `daily/management/commands/send_evening_plan_nudge.py`,
  `daily/README_THE_CLIMB.md`. Replace with the wins framing. **[NEW]**
- Jamie's identity: "here to cheer you on (and help when you're stuck)." User is the
  hero; Jamie is support.
- Keep onboarding short and modest. Do not over-promise; the quiet "let's just do one"
  is the whole vibe.

---

## 8. Data model impact (summary)

New or changed on `DailyParticipant`:
- `beta` (bool, default False): gates the ENTIRE new concept/UI (section 12 rollout).
  Non-beta users get the current app, completely unchanged. **[NEW]**
- `ai_mutations_enabled` (bool, default False): opt-in for AI list-curation WITHIN the new
  experience. (With the beta parallel run, no grandfather migration is needed, see section
  12; existing users stay on the current path which already runs the engine as today.) **[NEW]**
- (Managed-list fields `share_code` / `managed_by` are **[CUT]**, see section 6.)

New storage:
- The wins **backlog** cannot be `ChecklistVersion.questions` (that is the visible,
  all-at-once list capped at 20). It is its own list with a "surface one" selection
  rule. **[NEW]**

Reused as-is:
- `ChecklistVersion`, `DailyCheckIn`, `DailyCheckInAnswer`, `DailyAccessToken`,
  `CoachChatMessage`, the token-login flow, the bonus-reveal pattern.

Onboarding change:
- Stop auto-seeding `BASELINE_QUESTIONS`; new users start near-empty with the one-card
  flow.

---

## 8a. Security (design it in, per feature) [NEW]

Today's app is verified safe against SQL injection (ORM only, no raw SQL) and stored XSS
(template auto-escape, no `|safe`/`mark_safe`, and JS renders user content via
`textContent`, never `innerHTML`). Those protections come from framework defaults plus a
render discipline. The NEW features do NOT all inherit that safety for free.

**Text features (onboarding first item, wins backlog, bad-habit items): safe IF the
discipline holds.** Rule, non-negotiable in new code: store via the ORM, render via escaped
`{{ }}` or `textContent`, NEVER `innerHTML` / `|safe` / `mark_safe`. New surfaces to hold to
this: the win "crown," the "part of: goal" line, the backlog browse view, bad-habit rows.
The pattern is safe; the risk is a new template/JS breaking it.

**AI features (chat, win-identification, scoping): a DIFFERENT risk class.**
- Prompt injection: user free text flows into Jamie's prompt ("ignore your instructions").
  The guardrail preamble (3a) helps but does not fully solve it. Architectural mitigation:
  Jamie cannot take privileged actions (edit the list) without the explicit opt-in, keep it
  that way; never wire her output to an automatic privileged action.
- Rendering her output: Jamie's replies are stored and shown. Render them with the SAME
  textContent/escaped discipline; a jailbroken Jamie must not be able to emit executable
  HTML. Trust her text no more than the user's.

**Managed / sponsored lists: [CUT].** The multi-user delegation layer is removed (section
6), so the plan introduces NO new cross-user attack surface. The app stays single-user, and
the single-user authorization invariant below continues to hold for every new surface.

**Audit result (current single-user code, verified 2026-07): clean, no IDOR.** Identity is
bound to the server-side session (`SESSION_DAILY_PARTICIPANT_ID`, set only at token login),
never a client parameter; every query scopes to `request.daily_participant` (direct
`participant=`, joined `check_in__participant=participant`, or client keys validated against
the participant's OWN version); the one URL id (`respond_to_suggestion`) filters by
`check_in__participant=participant`; the one client `?t=` param is a validated bearer token.
This safety is LOAD-BEARING on the single-user invariant "one session = one participant,
everything scopes to it." Every NEW endpoint (wins backlog add/surface/did-it/defer, item
add) MUST keep this invariant: scope to `request.daily_participant`, never trust an owner id
from the client. With managed lists cut, nothing in the plan deliberately breaks it.

Bottom line: XSS/SQLi safety carries forward by DISCIPLINE (hold the render/ORM rules), and
the single-user authorization invariant carries forward by keeping every new endpoint scoped
to the session participant. No new multi-user authorization surface is introduced.

---

## 9. Instrumentation (required, not optional)

To understand how each facet is actually used (so we can improve each, NOT to pick a
winner) and to trigger the mutation-engine sunset, log events:
- `plan-created`, `item-checked`, `win-completed`, `win-deferred`,
  `evening-nudge-opened`, `mutation-applied`, `ai_mutations_enabled` value per user.
Both modes must also be **discoverable**, or low usage of one just means "buried," not
"unwanted," which leads to the opposite decision.

---

## 9a. Success metrics, hypotheses, and ambition

**Ambition (working assumption): start as B, keep A open. [OPEN, confirm]**
- A = venture-scale product (thousands of users, a business).
- B = a sustainable tool that delights a defined circle (Spencer, family, friends), happy
  at tens of loyal daily users.
- Key insight: at n=1 the near-term work is IDENTICAL either way. The path to A runs
  through B, you cannot scale a loop that does not yet retain a handful of people. So we
  commit to the B-shaped validation milestone now and treat A-vs-B as a fork that only
  matters AFTER validation. This defines success without a premature life decision.

**Near-term validation milestone (the current gate, the only thing that matters now):**
get from n=1 to ~5-10 users who use the app daily for 4+ weeks. That proves the loop
generalizes beyond Spencer. Everything downstream (marketing, scale) is premature until
this clears.

**North star (post-validation):** sustained behavior plus retention. Proposed single
number: % of new users still completing >=1 item on >=4 of 7 days by week 4. That is
"did they build a habit and did they stick around."

**Per-facet diagnostics (to improve each facet, NOT a scoreboard):**
- Repetition: item completions per active week; habit-list survival (do people hold a
  self-curated list over weeks).
- Wins: wins COMPLETED and completion RATE (done / created); wins that GRADUATE into
  habits (the deepest success signal). NEVER headline wins *created*.

**Guardrail / counter-metrics (watch the failure modes):**
- Wins created without completion = the avoidance trap showing up in the data. A bloating
  backlog with low completion means the wins facet is failing even if "engagement" looks
  busy.
- Churn or overwhelm right after a user adds many items = the onboarding-simplicity bet
  breaking.

**Hypotheses with kill signals (this is what makes it falsifiable):**
- H1: people sustain a small self-curated habit list daily. Support: Spencer. Confirm:
  more users hold a list 4+ weeks.
- H2 (the bet): people COMPLETE meaningful put-off wins rather than hoard them. Kill:
  completion rate stays low and backlogs bloat.
- H3: doing both facets retains better than either alone.
- (H4 "a sponsor link raises the doer's completion" is **[CUT]** with managed lists.)

All of these depend on the section 9 events existing first.

---

## 10. Open questions for the reviewer

1. **Wins backlog cap:** hard 100, higher, or effectively uncapped (since never fully
   rendered)?
2. **Peek:** always-on faint peek vs reveal-after-win (recommendation: reveal-after-win)?
3. **"Already did it" on add:** composer toggle vs Jamie-voiced inline nudge?
4. **Wins selection order:** user-ordered (recommended) vs Jamie-picks vs oldest-first?
5. **Streaks:** confirm per-day (recommended) over per-item.
6. **Sunset threshold N** for removing the mutation engine.
7. **Spencer diagnostics (needed before finalizing):** (a) is he on `ai_mutations_enabled`
   (does the engine retain our best user); (b) bridged warrior or general external user
   (which audience owns our only validation); (c) does he use Jamie/chat at all, or is he
   a pure self-tracker (if he ignores Jamie, the AI is not the retention driver, the
   tracking is, and investment should shift accordingly); (d) what actually brings him
   back daily (the empirical retention mechanic to build around)? His focus would be
   "health."
8. **Beachhead:** which concrete wedge for general self-improvement (the "parents helping
   adult kids" delegation wedge is [CUT] with managed lists)? And what is the acquisition
   channel for general users (none exists yet)?
9. **Focus wording:** "My health" / "My life" labels, and whether focus is truly binary
   or needs a lightweight third path. Avoid a "Both" default (reintroduces muddiness).
10. **Success metric / core hypothesis:** DEFINED in section 9a (near-term validation
    milestone, north star, per-facet diagnostics, guardrails, H1-H4 with kill signals).
    Still to confirm: the ambition working-assumption (start B / keep A open) and the
    exact numeric targets.

Resolved during planning (no longer open): the "frog" replacement (now "today's win"),
Jamie coach-vs-support (she stays ONE consistent supporter/cheerleader, "here to cheer
you on"; focus only changes how substantive her guidance is; her list-editing power is a
separate opt-in), and Branch A cold-start (the focus question is the personalizing input).

---

## 11. Biggest risks to pressure-test

- **One product, two facets, could read as two bolted-on lists.** The risk is NOT "which
  mode wins" (both are integral). It is coherence: the habit checklist and the wins
  surface must feel like one product on one daily screen. Designed in section 2c (one
  list, win as crown, habits as body). Still needs validation with real users that it
  actually reads as one thing.
- **Wins framing may go soft.** The synthesis keeps an anti-avoidance engine underneath
  precisely to stop "wins" from becoming a feel-good list where the hard thing never
  gets done. Verify the defer-guard and Jamie's "anything bigger?" probe actually fire.
- **Gated legacy engine is maintenance debt.** It stays until data says remove it. If
  the sunset metric is never watched, the complexity is permanent.
- **Onboarding scope creep.** Every added question erodes the "just put one thing down"
  simplicity that is the core bet.

---

## 12. Phasing and MVP sequence

**Rollout strategy: parallel run behind a `beta` flag. [DECIDED]** Do NOT modify the
current UI or code path. Build the new concept (new onboarding, combined screen, wins
facet, narrowed Jamie) as SEPARATE templates/views, shown ONLY to participants with
`beta = True`. Everyone else (default, including Spencer) sees exactly today's app,
unchanged. Set `beta` per user (admin) to recruit testers.
Why this is the safest rollout:
- Zero risk to current users and the one validated user, their code path is untouched.
- SUPERSEDES the mutation-engine grandfathering/migration (section 3): existing users stay
  on the current path (already runs the engine as today), so there is no flag migration.
  `ai_mutations_enabled` is an opt-in WITHIN the new experience only.
- Simpler sunset: retire the old UI + engine WHOLESALE once the new concept validates and
  users migrate, instead of watching a usage threshold. If the new concept fails to
  validate, delete the beta code, nothing else was touched.
- Cost: two UIs coexist temporarily, but the old one is FROZEN (no edits), so cost is low.
  New backend additions (wins backlog storage, new flags) are additive and ignored by the
  old path.

Principle: prove the loop retains a handful of people (the section 9a milestone) BEFORE
building the unproven or the ambitious. Instrument first, validate the known-good core,
then test the differentiator, then expand. Do not reorder.

**Phase 0 (now, ~zero product change): instrument and start the clock.**
Ship the section 9 events on the EXISTING app. Begin measuring Spencer and recruit 2-3
more real users. Starts the data clock immediately and de-risks everything, no build
required. Also answer the Spencer diagnostics (section 10 #8) from real data.

**Phase 1 (the validated MVP, behind `beta`): the clean habit core.**
- Built as new templates/views shown only to `beta = True` users; current app untouched.
- One-card onboarding plus the focus question (health/life). [NEW]
- Narrowed support-only Jamie as the default in the new experience (`ai_mutations_enabled`
  off; opt-in only). No migration for existing users, they stay on the current path. [NEW]
- Jamie safety layer (section 3a): shared guardrail preamble in every prompt plus crisis
  detection with a vetted response. REQUIRED as soon as any general user can chat. [NEW]
- Copy sweep: scrape "frog," remove em-dashes. [NEW]
- (Already shipped: the "?" help button.)
- Goal: recruit 5-10 users, hit "daily for 4+ weeks." This is the validation gate.
- Deliberately does NOT include wins. Prove the KNOWN-good loop (habits plus metrics)
  retains more than one person before betting build effort on the unproven facet.

**Phase 2 (test the differentiator): the wins facet.**
- New backlog storage (separate from the 20-cap checklist), surface-one selection, Jamie
  scoping, defer-not-delete, the win "crown" on the combined screen (section 2c),
  win-to-habit graduation. [NEW]
- Introduce to the retained Phase-1 users. Measure H2 (completion, not hoarding). This is
  where the one-product thesis actually gets tested.

**Phase 3 (managed / sponsored lists): [CUT].** The delegation layer is removed from the
product (section 6). The Climb stays a single-user personal tracker. Nothing replaces this
phase; after Phase 2 the roadmap is the post-validation future facets (section 13).

**Ongoing: sunset the mutation engine.** Watch its usage (section 3); remove when it hits
the agreed threshold.

Dependencies: instrumentation (Phase 0) gates measurement of every later phase.

---

## 13. Future facets (captured, post-validation, NOT MVP)

Good ideas parked deliberately so they do not cause scope creep before the core validates.

### 13a. Breaking bad habits ("do less bad")

Completes the behavior-change picture: do more good (habits), do the hard meaningful thing
(wins), and **do less bad** (break habits). Strengthens the "help you do what matters"
thesis. But it is another unproven facet (n=0), so it is explicitly post-validation, NOT
Phase 1 or 2.

Why it is not just another habit item, it has different mechanics:
- **Inverted tracking (success = absence).** A good habit is "I did it." A bad habit is
  "I did NOT do it" / "I stayed clean today." Track it as a clean-day check or a threshold
  ("kept scrolling under 30 min"), not the same "did it" checkbox.
- **Relapse compassion is make-or-break.** Do NOT build a brutal "0 days since" counter
  that resets on any slip, that triggers the documented "what-the-hell effect" (one slip,
  might as well binge). Count clean days cumulatively, treat a slip as data not failure,
  and have Jamie respond "one slip is just life, here's tomorrow" rather than resetting a
  shame counter. This single choice decides whether the feature helps or harms.
- **Replacement, not just subtraction.** "Stop doom-scrolling" fails; "when you reach for
  your phone, read 10 minutes instead" works. A bad-habit item ideally pairs with a
  substitute good habit, so it connects back to the repetition facet.

Structure: model as an **"avoid" item-type inside the habit checklist**, not a whole new
mode, so the product stays one thing. It carries its own inverted tracking and gentler
tone.

**Safety boundary (must be in Jamie's prompts).** "Bad habits" spans a huge range. Everyday
ones (doom-scrolling, late snacking, nail-biting, snooze) are in Jamie's lane. Real
dependency (alcohol, nicotine, drugs), self-harm, and eating disorders are CLINICAL and out
of lane. Jamie must not play therapist or addiction counselor; for anything that reads as
genuine dependency or self-harm she gently points to real help rather than pretend to treat
it. Without this guardrail the feature is a liability.

### 13b. Smaller enhancements (parked, not facets)

Engineering hardening and polish captured so they are not forgotten, but not worth
building before the core validates.

- **Label-level dedup guard on AI auto-suggest.** When the user taps "Auto" (Add habit
  or swap), the model is already sent the full current list and told not to duplicate or
  near-duplicate it, and the view refuses an item whose KEY collides. But that hard guard
  is key-level only, so a semantic duplicate with a fresh key ("Walked 20 min" vs "Went
  for a walk") is caught by the prompt, not by code. Enhancement: normalize labels
  (lowercase, trim, collapse whitespace) and reject an exact normalized match against any
  existing item, retrying once for a fresh suggestion. Closes the gap between "the prompt
  usually avoids duplicates" and "duplicates cannot land." Low effort; do it if users
  report repeated suggestions.
