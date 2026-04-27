# Deployment Guide

## Architecture Overview

```
strongasan0x.com
    |
    v
[DigitalOcean VPS: leviathan-meatpacking]
    - Apache + mod_wsgi serves the Django web app
    - Discord bot runs here (TODO: confirm)
    - Path: /var/www/ox/strongasan0x/
    - Venv: /var/www/ox/strongasan0x/ox-env/
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

## Deploying Updates

### Web app (server)

```bash
ssh leviathan-meatpacking
cd /var/www/ox/strongasan0x
source ox-env/bin/activate
git pull
pip install -r requirements.txt    # only if dependencies changed
python manage.py migrate            # only if there are new migrations
sudo systemctl restart apache2
```

### Telegram bot (Mac Mini)

```bash
cd ~/dev/ox/strongasan0x
source ox-env/bin/activate
git pull
pip install -r requirements.txt    # only if dependencies changed
python manage.py migrate            # only if there are new migrations
# Stop the running bot (Ctrl+C or kill the process), then:
python manage.py run_telegram_bot
```

### Quick deploy (no dependency or migration changes)

Server:
```bash
ssh leviathan-meatpacking
cd /var/www/ox/strongasan0x && git pull && sudo systemctl restart apache2
```

Mac Mini:
```bash
cd ~/dev/ox/strongasan0x && git pull
# Restart the Telegram bot
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
- Migrations are managed via Django — always run `python manage.py migrate` after pulling if `showmigrations` shows unapplied migrations

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
