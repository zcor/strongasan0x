# Metrics tracking — design (for sign-off before building)

Serves the **Spencer persona** (data/measurement) without touching the **Amy
persona** (dopamine/simplicity). Per the governing principle: dormant for Amy,
subtle + opt-in for Spencer, never default clutter.

## What Spencer actually logs (from his real comment history)

| Metric | Format he uses | Type |
|---|---|---|
| Bodyweight | `Weight: 184` / `186.2` | decimal, 1/day |
| Grip L / R | `left-145, right-156` | int (lbs), paired |
| Back pain | `2/10 morning, 3/10 end of day` | int 0-10, AM/PM |
| (wants) Resting HR / HRV | "log heart rate in morning" | int |

So the system must handle: **decimals, paired values, AM/PM (multiple readings
per day), and arbitrary user-defined metrics.** Design for that generality now
so charts are a thin layer later.

## Data model (the part to get right — build now)

```
DailyMetric                      # a metric DEFINITION, per participant
  participant   FK
  key           slug   e.g. "bodyweight", "grip_left", "back_pain"
  label         str    "Bodyweight"
  unit          str    "lbs", "/10", "" 
  kind          enum   number | paired | scale   (scale = 0-10)
  is_active     bool
  sort_order    int

DailyMetricReading               # one logged value
  metric        FK
  date          date
  value         decimal
  slot          str    "" | "am" | "pm"   (for pain morning/evening)
  created_at
  unique (metric, date, slot)
```

Why this shape:
- **Per-participant definitions** → Spencer gets weight/grip/pain; Amy has
  NONE, so nothing renders for her. Persona branch falls out of the data.
- **decimal value + slot** covers weight (one), grip (two metrics: L/R), and
  pain (one metric, am+pm slots) without special-casing.
- Charts later = `SELECT date, value FROM readings WHERE metric=X ORDER BY date`
  → a sparkline. The hard part (clean longitudinal data) is done up front.

## Phase 1 — what I build first (minimal, ships this week)

**Entry UX = structured quick-fields (your option 4).** Below the comment box,
for participants who HAVE active metrics, render a compact row of labeled
number inputs (Weight ▢, Grip L ▢ R ▢, Pain AM ▢ PM ▢). Saving writes
DailyMetricReading rows. Amy has no active metrics → the whole block is absent.

How metrics get ACTIVATED (no manual admin):
- **Backfill from history**: a one-time command parses existing comments
  (the regex above already works) → seeds Spencer's metrics + past readings,
  so his charts have 2 weeks of data on day one.
- **Coach-offered**: when the coach sees number-logging in comments, it can
  propose activating a metric (a subtle yes/no), keeping it self-activating
  per persona.

Main screen stays byte-identical for Amy. For Spencer, it's a small, clearly
optional input row under the comment — not new "sections," just structured
capture of what he's already typing.

## Phase 2 — charts (fast, because the data model is right)

A subtle "📈 trends" affordance (only shown when the user has readings) → a
trends view: one sparkline per metric, simple SVG, same dark theme. No new
deps. This is a thin read-layer over DailyMetricReading.

## Simplicity-principle checklist
- [x] Amy's screen unchanged (no active metrics = nothing renders)
- [x] Spencer's addition is optional + subtle (a quiet input row, off by default)
- [x] No new core-screen "feature" competing for attention
- [x] Self-activating per persona (backfill + coach offer), no manual tagging
- [x] Architected for charts now so Phase 2 is thin

## Open questions for sign-off
1. Phase 1 = quick-fields only, charts in Phase 2? (Or build both together?)
2. Backfill Spencer's history immediately so he has instant charts? (Yes recommended.)
3. Grip as two metrics (L/R) or one paired metric? (Leaning two — simpler model, charts each side.)
