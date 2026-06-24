# Cron jobs for strongasan0x (ox). Standard /etc/cron.d format.
# Installed to /etc/cron.d/ox-strongasan0x by the ox-deploy wrapper on every
# deploy — so scheduling a job is a reviewable code change, no extra privilege.
# Times are server-local. Runs as zcor (owns the repo + venv).

# Daily home-screen badge push: refresh each device's "to-dos left" count at
# 06:30, while the app is closed (the only way to update an iOS PWA badge).
# Logs to the repo's logs/ dir — zcor CANNOT write /var/log (root-owned 755),
# which silently failed the redirect and stopped this cron from EVER running.
# mkdir -p inline so it self-heals whether or not the dir exists yet.
30 6 * * * zcor mkdir -p /var/www/ox/strongasan0x/logs && /var/www/ox/strongasan0x/ox-env/bin/python /var/www/ox/strongasan0x/manage.py send_daily_badges >> /var/www/ox/strongasan0x/logs/cron-badges.log 2>&1
