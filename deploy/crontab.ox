# Cron jobs for strongasan0x (ox). Standard /etc/cron.d format.
# Installed to /etc/cron.d/ox-strongasan0x by the ox-deploy wrapper on every
# deploy — so scheduling a job is a reviewable code change, no extra privilege.
# Times are server-local. Runs as zcor (owns the repo + venv).
#
# Both jobs run HOURLY and resolve each participant's OWN timezone internally,
# acting once per that user's local day. This fixes the single-server-tz bug:
# a fixed 06:30 server-time job fired at 11:30 PM Pacific for the prior day and
# never matched a non-Pacific user's morning. Each command is idempotent, so
# the 23 hourly runs that aren't a given user's morning/midnight just no-op.
#
# ORDER WITHIN THE HOUR MATTERS: the coach job runs at :00, the badge job at
# :10. "Smart morning timing" (daily.services.tz.target_morning_hour) can push
# a participant's target hour as early as their local midnight (learned wake
# hour minus one, floored at 0) — the SAME hour run_coach_for_all needs to
# have processed "yesterday" in for that participant. Coach-before-badge
# guarantees the overnight note + any checklist mutation are in place before
# that hour's badge push reads the checklist. (Belt-and-suspenders: the lazy
# in-request path in views.py also catches up synchronously if a user opens
# the app before either cron tick, so this ordering is a freshness guarantee
# for the push, not a correctness requirement for the app itself.)
#
# Logs go to the repo's logs/ dir — zcor CANNOT write /var/log (root-owned
# 755), which silently failed the redirect and stopped cron from EVER running.
# mkdir -p inline so it self-heals whether or not the dir exists yet.

# Overnight coach: for each active participant, coach their most recent
# un-coached day (the day that just ended in THEIR tz). Makes the coach's
# "your list updates overnight" promise real — chat requests get read + applied
# without the user reopening the app. Idempotent: a coached day is skipped, so
# the first hourly run after a user's local midnight coaches, the rest no-op.
# Runs BEFORE the badge job (see ORDER note above).
0 * * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py run_coach_for_all >> /var/www/ox/strongasan0x/logs/cron-coach.log 2>&1

# Home-screen badge push: refresh each device's "to-dos left" count at each
# participant's own smart-morning-timing target hour — their learned typical
# wake hour minus one (falling back to 6am local for thin-data users), so the
# push lands just before they wake. --hourly = fire per-user at their target
# hour, once per local day. Runs AFTER the coach job (see ORDER note above).
10 * * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py send_daily_badges --hourly >> /var/www/ox/strongasan0x/logs/cron-badges.log 2>&1

# Evening "Plan tomorrow" nudge: OPT-IN per user. Fires only for participants
# who have DailyParticipant.evening_nudge_hour set, at that local hour, once
# per local day, inviting them to set tomorrow's 3 items via the "Plan
# tomorrow" chat flow. A null override = no nudge (no blanket default — 10pm
# was wrong for night owls). Fully independent of the badge job above — its
# own dedupe field (PushSubscription.last_evening_nudge_date), so neither job
# can suppress the other. Skips anyone who already planned tomorrow. Offset
# to :20 so it doesn't contend with the coach (:00) / badge (:10) jobs.
20 * * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py send_evening_plan_nudge --hourly >> /var/www/ox/strongasan0x/logs/cron-evening-nudge.log 2>&1
