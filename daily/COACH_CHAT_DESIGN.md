# Coach Chat + layout redesign — architecture (for sign-off)

Combines: items-hero layout (Option B) + ring top-right (Option A header) +
the coach becoming a live two-way CHAT that replaces the comment box. The
comment box was the #1 feedback channel — the chat is its richer successor.

## Layout (the fit fix)

```
┌──────────────────────────────────┐
│ Hi, CurveCap                ╭───╮ │  ring floats top-RIGHT, compact (72px)
│ Fri Jun 19                  │2/5│ │  greeting/date stay, lightweight
│                             ╰───╯ │
│ 🔥6  ·  S S M T W [T] F          │  streak + week strip = one slim row
├──────────────────────────────────┤
│ ◯ Kept eating window ≤8h         │  THE 5 ITEMS — now the hero, top of page
│ ◯ 10 min mobility                │  all visible above the fold
│ ◯ Slept 7+ hours                 │
│ ◯ Got outside in the sun         │
│ ◯ Hot bath / cold plunge         │
│   (bonus appears here when earned)│
│ + log: weight ▢ grip ▢▢ pain ▢▢  │  metrics (Spencer only) — compact inline
└──────────────────────────────────┘
                            ╭─────╮
                            │ 💬  │  ← Coach FAB, bottom-right, floating
                            ╰─────╯     (badge dot when coach has a new reply)
```
The comment box and coach-note card LEAVE the vertical stack → into the FAB
overlay. That's what reclaims the space: ~300px of header/note/comment gone
from the scroll, items rise to the top.

## The Coach: three states (shared-element transition)

1. **On load** — if there's a fresh coach note, it presents as a centered
   MODAL card, app dimmed behind it ("Coach's note" + the message). Tapping
   "Got it" animates the card DOWN-RIGHT, shrinking into the FAB (the
   shared-element / "genie" feel). If no fresh note, just the FAB sits quietly.
2. **FAB (resting)** — a 💬 circle bottom-right. A small dot when the coach has
   something unread.
3. **Tap FAB → CHAT** — the FAB expands (transform + border-radius transition)
   into a full-height messaging panel: scrollable history (coach left, user
   right, like Messages), a text input + Send button pinned at the bottom.
   Close (chevron/down) contracts it back into the FAB.

The expand/contract is ONE element animating between a 56px circle and a
full panel via `transform: scale` + `border-radius` + `opacity`, 0.3s
cubic-bezier — the native iOS bubble feel. Respects prefers-reduced-motion
(cross-fade instead).

## Data model

```
CoachChatMessage
  participant   FK
  role          'user' | 'coach'
  text          text
  date          date          # which check-in day it belongs to
  created_at
  # coach replies may carry an applied checklist change (reuse the existing
  # CoachSuggestion mutation path) — link optional:
  suggestion    FK CoachSuggestion null  # if this reply changed the list
```
The morning note becomes the first `coach` message of the day (migrated from
CoachSuggestion.suggestion_text). User messages REPLACE the old comment field
as the feedback channel — same content, richer surface.

## Live chat backend

- `POST /daily/chat/` {text} → save user message → call a NEW conversational
  coach (`chat_reply()` in ai_coach.py): system prompt = "you are Coach Jamie,
  in a live chat; you can see today's checklist + their logged metrics +
  recent history; reply briefly and warmly; if they ask for a checklist
  change, apply it") + the last N messages as conversation turns → save coach
  reply → return it. Reuses the DeepSeek client already in ai_coach.py.
- The coach reply CAN still mutate the checklist (e.g. "make my walk 15 min")
  by reusing generate_one_bonus / the swap path — so chat keeps the agency the
  comment box had.

## Feedback continuity (your call: chat-only + notify you)

- User chat messages are THE feedback channel now (the comment box retires).
  Same data, queryable the same way you read comments today.
- **Notify you**: each user message fires a Telegram DM to you (tg_id
  1234982301) via the bot token — "💬 Spencer: 'can we change walk to 15 min'".
  So you never miss the feedback you've been catching by hand. Throttled/
  batched so a chatty day isn't a DM flood.

## Build order (all at once, per your call)
1. Models + migration (CoachChatMessage)
2. chat_reply() service + /daily/chat/ endpoint + Telegram notify
3. Layout rebuild (items hero, ring top-right, slim streak/week row)
4. Coach FAB + modal + expanding chat panel (the shared-element animation)
5. Seed today's coach note as the first chat message; retire the comment box
6. Deploy + you test on phone

## Simplicity-principle check
- Main screen gets SIMPLER (note + comment box leave it; items rise to top).
- The chat is opt-in depth behind one tap — calm by default.
- Net: less on screen, more capability, exactly one new interaction (the FAB).

## Open questions
1. Notify you of EVERY user message, or only ones that look like feedback/
   requests (vs. "weight 184")? (Lean: all, lightly — you've valued seeing it all.)
2. Coach replies live on every message (cost: 1 DeepSeek call each) — fine?
   (Cheap on deepseek-chat; ~$0.0003/reply.)
3. Keep metrics inline on the main screen, or also move them into the chat?
   (Lean: keep inline — they're quick taps, not conversation.)
