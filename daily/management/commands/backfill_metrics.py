"""Seed a participant's metrics + historical readings from their free-text
check-in comments. Activates the metrics quick-entry UI for data-loggers
(Spencer persona) with their real history already in place, so charts have
data on day one.

Parses: Weight, Grip (left/right), Back pain (am/pm 0-10). Idempotent —
re-running updates readings, never duplicates.

Usage:
    python manage.py backfill_metrics --participant 10
    python manage.py backfill_metrics --participant 10 --dry-run
"""
import re
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from daily.models import DailyCheckIn, DailyMetric, DailyMetricReading, DailyParticipant

# Metric definitions we know how to extract. (key, label, unit, kind, has_am_pm, sort)
METRIC_DEFS = [
    ("bodyweight", "Bodyweight", "lbs", DailyMetric.KIND_NUMBER, False, 10),
    ("grip_left", "Grip (left)", "lbs", DailyMetric.KIND_NUMBER, False, 20),
    ("grip_right", "Grip (right)", "lbs", DailyMetric.KIND_NUMBER, False, 30),
    ("back_pain", "Back pain", "/10", DailyMetric.KIND_SCALE, True, 40),
    ("bp_systolic", "Blood pressure (systolic)", "mmHg", DailyMetric.KIND_NUMBER, False, 50),
    ("bp_diastolic", "Blood pressure (diastolic)", "mmHg", DailyMetric.KIND_NUMBER, False, 60),
    ("resting_hr", "Resting heart rate", "bpm", DailyMetric.KIND_NUMBER, False, 70),
]


def _dec(s):
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError):
        return None


def parse_comment(text):
    """Return {(key, slot): Decimal} extracted from one comment."""
    out = {}
    wt = re.search(r"[Ww]eight:?\s*([0-9]+(?:\.[0-9]+)?)", text)
    if wt and _dec(wt.group(1)) is not None:
        out[("bodyweight", "")] = _dec(wt.group(1))
    gl = re.search(r"left[:\s-]*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if gl and _dec(gl.group(1)) is not None:
        out[("grip_left", "")] = _dec(gl.group(1))
    gr = re.search(r"right[:\s-]*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if gr and _dec(gr.group(1)) is not None:
        out[("grip_right", "")] = _dec(gr.group(1))
    # Blood pressure: "129/78" (systolic/diastolic). Match sys 80-220, dia
    # 40-140 so it doesn't catch pain ("2/10") or dates. Do BP BEFORE pain so
    # the BP numerator isn't mistaken for a pain score.
    bp = re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", text)
    if bp:
        s, d = int(bp.group(1)), int(bp.group(2))
        if 80 <= s <= 220 and 40 <= d <= 140:
            out[("bp_systolic", "")] = _dec(s)
            out[("bp_diastolic", "")] = _dec(d)
    # Resting / heart rate: "64 bpm", "hr 64", "heart rate 64".
    hr = re.search(r"(?:(?:resting\s*)?(?:heart\s*rate|hr)\s*[:\-]?\s*(\d{2,3})|(\d{2,3})\s*bpm)", text, re.I)
    if hr:
        val = hr.group(1) or hr.group(2)
        if val and 30 <= int(val) <= 220:
            out[("resting_hr", "")] = _dec(val)
    # Back pain: "2/10 morning ... 3/10 ... end of day". Take first as AM,
    # second (if any) as PM. Best-effort — pain mentions vary. Only "/10".
    pains = re.findall(r"([0-9]+)\s*/\s*10\b", text)
    if pains:
        out[("back_pain", "am")] = _dec(pains[0])
        if len(pains) > 1:
            out[("back_pain", "pm")] = _dec(pains[1])
    return out


class Command(BaseCommand):
    help = "Seed metrics + readings from a participant's check-in comments."

    def add_arguments(self, parser):
        parser.add_argument("--participant", type=int, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        pid = opts["participant"]
        try:
            participant = DailyParticipant.objects.get(id=pid)
        except DailyParticipant.DoesNotExist:
            self.stderr.write(f"No participant {pid}"); return

        checkins = list(
            DailyCheckIn.objects.filter(participant=participant)
            .exclude(comment="").order_by("date")
        )
        # Which metric keys actually appear in their history?
        seen_keys = set()
        parsed_by_date = {}
        for ci in checkins:
            vals = parse_comment(ci.comment or "")
            if vals:
                parsed_by_date[ci.date] = vals
                seen_keys.update(k for (k, _slot) in vals)

        if not seen_keys:
            self.stdout.write("No parseable metrics in this participant's comments.")
            return

        self.stdout.write(f"Found metrics: {sorted(seen_keys)} across {len(parsed_by_date)} days")
        if opts["dry_run"]:
            for d in sorted(parsed_by_date):
                self.stdout.write(f"  {d}: {parsed_by_date[d]}")
            return

        # 1) Create metric definitions (only the ones seen).
        defs = {d[0]: d for d in METRIC_DEFS}
        metric_objs = {}
        for key in sorted(seen_keys):
            if key not in defs:
                continue
            _, label, unit, kind, am_pm, sort = defs[key]
            m, _ = DailyMetric.objects.update_or_create(
                participant=participant, key=key,
                defaults={"label": label, "unit": unit, "kind": kind,
                          "has_am_pm": am_pm, "is_active": True, "sort_order": sort},
            )
            metric_objs[key] = m

        # 2) Backfill readings.
        n = 0
        for d, vals in parsed_by_date.items():
            for (key, slot), value in vals.items():
                m = metric_objs.get(key)
                if m is None or value is None:
                    continue
                DailyMetricReading.objects.update_or_create(
                    metric=m, date=d, slot=slot, defaults={"value": value},
                )
                n += 1
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(metric_objs)} metrics, {n} readings for {participant.display_name}."
        ))
