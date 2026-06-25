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
# Logs go to the repo's logs/ dir — zcor CANNOT write /var/log (root-owned
# 755), which silently failed the redirect and stopped cron from EVER running.
# mkdir -p inline so it self-heals whether or not the dir exists yet.

# Home-screen badge push: refresh each device's "to-dos left" count at ~6am in
# the user's OWN timezone (the only way to update an iOS PWA badge while the app
# is closed). --hourly = fire per-user at their local morning, once per day.
0 * * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py send_daily_badges --hourly >> /var/www/ox/strongasan0x/logs/cron-badges.log 2>&1

# Overnight coach: for each active participant, coach their most recent
# un-coached day (the day that just ended in THEIR tz). Makes the coach's
# "your list updates overnight" promise real — chat requests get read + applied
# without the user reopening the app. Idempotent: a coached day is skipped, so
# the first hourly run after a user's local midnight coaches, the rest no-op.
15 * * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py run_coach_for_all >> /var/www/ox/strongasan0x/logs/cron-coach.log 2>&1
