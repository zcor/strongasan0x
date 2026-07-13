# Deployment Guide

## Architecture Overview

```
strongasan0x.com
    |
    v
[DigitalOcean VPS — SSH host alias: leviathan-api  (api.leviathannews.xyz)]
    - Apache + mod_wsgi serves the Django web app
    - Discord bot runs here (TODO: confirm)
    - Path: /var/www/ox/strongasan0x/  (repo owner: zcor:webdev)
    - Venv: /var/www/ox/strongasan0x/ox-env/  (NOT auto-activated; the deploy
      wrapper calls it by full path)
    |
    v
[DigitalOcean Managed PostgreSQL]
    - Host: leviathan-db-do-user-1161786-0.c.db.ondigitalocean.com
    - Port: 25060
    - Database: ox
    - User: ox_user
    ^
    |
[Mac Mini (Gerrit's local machine)]
    - Telegram bot runs here
    - Path: ~/dev/ox/strongasan0x/
    - Venv: ~/dev/ox/strongasan0x/ox-env/
```

Both the web app and the Telegram bot connect to the same remote database.

## Legacy Deployment

The closed-source predecessor (`zcor/0xfitness`) still lives at:
- Server: `/var/www/ox/0xfitness/`
- Mac Mini: `~/dev/ox/0xfitness/`

This is still used for the Garmin dashboard / weekly attestation generation. It shares the same database. Once that functionality is migrated or no longer needed, the old deployment can be removed.

## Server Details

### Web App (Apache + mod_wsgi)

- **Config file**: `/etc/apache2/sites-enabled/z-0xfitness.conf`
- **WSGI entry point**: `/var/www/ox/strongasan0x/strongasan0x/wsgi.py`
- **Static files**: Served directly via Apache `Alias` from `/var/www/ox/strongasan0x/rollcall/static/`
- **SSL**: Let's Encrypt, auto-managed via certbot
- **Logs**:
  - Error: `/var/log/apache2/0xfitness-ssl-error.log`
  - Access: `/var/log/apache2/0xfitness-ssl-access.log`

### Telegram Bot (Mac Mini)

- Runs as a long-lived process (polling mode)
- Start: `python manage.py run_telegram_bot`

## Source of truth for the `daily/` app

The `daily/` ("The Climb" / Daily) app is developed **directly in this repo**
(`zcor/strongasan0x`). The separate `zcor/daily-climb` repo was a temporary
extracted working copy used during the Climb reframe; its work was ported here
via PRs #45 and #47 and it is being retired as an authoring repo. Do not start
new `daily/` work in daily-climb: branch here, PR against this repo's `main`,
merge, deploy. There is no automatic sync between the two repos (they share no
git history), so anything left in daily-climb must be hand-ported until it is
fully retired.

## Deploying Updates

### Web app (server) — the ONLY supported path

Deploy with the vetted root-owned wrapper. One command does everything:
sync the tree to `origin/main` (hard reset, so the deploy tree must never be
hand-edited), `pip install`, `migrate --noinput`, sync cron from
`deploy/crontab.ox`, and restart Apache:

```bash
ssh leviathan-api "sudo -n /opt/ox/deploy/ox-deploy"
```

Notes:
- **Merge to `main` first.** The wrapper deploys `origin/main`, not a branch or
  a dirty tree. Workflow: branch, PR, wait for CI green, `gh pr merge`, deploy.
- Migrations run automatically inside the wrapper. There is no separate manual
  `migrate` step and no `source ox-env/bin/activate` (the wrapper invokes the
  venv Python by full path as the repo owner `zcor`).
- Do NOT `git pull` / edit files directly in `/var/www/ox/strongasan0x` — the
  wrapper's `git reset --hard origin/main` will discard any local changes.

Run one allowlisted management command on demand (badges, backfills, overnight
coach, participant pre-build, set_beta):

```bash
ssh leviathan-api "sudo -n /opt/ox/deploy/ox-run <command> [args]"
```

The `ox-run` allowlist is baked into `/opt/ox/deploy/ox-run`. Adding a command
means editing `deploy/wrappers/ox-deploy-setup.sh` AND re-running that setup
script as root on the box (`sudo bash .../ox-deploy-setup.sh`) — a plain deploy
does not refresh the wrapper.

### Telegram bot (Mac Mini)

The Telegram bot runs on the Mac Mini, separate from the server deploy:

```bash
cd ~/dev/ox/strongasan0x
source ox-env/bin/activate
git pull
pip install -r requirements.txt    # only if dependencies changed
python manage.py migrate            # only if there are new migrations
# Stop the running bot (Ctrl+C or kill the process), then:
python manage.py run_telegram_bot
```

## Environment Variables

Both deployments use a `.env` file in the project root. See `.env.example` for all variables.

Key variables that were added during the open-source migration (not in the old deployment):
- `SECRET_KEY` — Django secret key (was previously hardcoded)
- `ALLOWED_HOSTS` — Server should include `strongasan0x.com,www.strongasan0x.com,localhost,127.0.0.1`

## Database Notes

- The database is shared between all deployments (web, bots, legacy)
- Tables use the `rollcall_` prefix (renamed from `garmin_data_` during open-source migration)
- The legacy `0xfitness` deployment still expects `garmin_data_` prefixed tables, so if you remove the legacy deployment, no action needed; if you need to run both simultaneously, the legacy one will break on renamed tables
- Migrations are managed via Django. On the SERVER they run automatically inside `ox-deploy` (no manual step). On the Mac Mini bot checkout, run `python manage.py migrate` after pulling if `showmigrations` shows unapplied migrations.
- Current daily-app schema head: `0015_dailycheckinanswer_derived` (0012–0015 applied in prod as of the Climb beta deploy). There is no 0016.

## Troubleshooting

### 500 error with no details in logs
Set `DEBUG=True` in `.env`, restart Apache, reproduce the error, then **set it back to `False`**.

### Apache won't start after config change
Check syntax: `sudo apache2ctl configtest`

### "relation rollcall_xxx does not exist"
Tables may not have been renamed. Check with:
```bash
python manage.py shell -c "from django.db import connection; c = connection.cursor(); c.execute(\"SELECT tablename FROM pg_tables WHERE tablename LIKE 'garmin_data_%%'\"); print(c.fetchall())"
```

### Bot can't connect to database
Verify `.env` has the correct `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`. The managed DB requires SSL — make sure `psycopg2` is installed (not `psycopg2-binary` in some cases).
