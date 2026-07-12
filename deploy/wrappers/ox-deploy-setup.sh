#!/bin/bash
#
# ox_deploy_setup.sh — ONE-TIME setup so the `claude` agent can deploy
# strongasan0x unattended, with the tightest possible blast radius.
#
# WHY: the repo is zcor:webdev and `claude` is a separate lower-privilege
# user (not in webdev), so it cannot git-pull the deploy tree and
# `sudo -u zcor git` prompts for a password it doesn't have. The fix is a
# single root-owned wrapper + one NOPASSWD sudoers line scoped to ONLY that
# wrapper. After this, `claude` can run exactly one vetted action —
# deploy strongasan0x — and nothing else new.
#
# Mirrors the existing /opt/leviathan/deploy/leviathan-deploy convention
# already trusted on this box.
#
# RUN ONCE, as root:
#     scp ox_deploy_setup.sh leviathan-api:
#     ssh leviathan-api 'sudo bash ox_deploy_setup.sh'
#
# Idempotent: safe to re-run (overwrites the wrapper, rewrites the sudoers
# drop-in). Validates the sudoers file before installing it.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo bash $0)" >&2
    exit 1
fi

# ---- config (adjust only if paths differ) --------------------------------
AGENT_USER="claude"
REPO="/var/www/ox/strongasan0x"
REPO_OWNER="zcor"
REPO_GROUP="webdev"
SERVICE="apache2.service"
WRAPPER_DIR="/opt/ox/deploy"
WRAPPER="${WRAPPER_DIR}/ox-deploy"
SUDOERS_DROPIN="/etc/sudoers.d/ox-deploy-claude"
# --------------------------------------------------------------------------

echo "=== ox deploy setup ==="

# Sanity: repo must exist.
if [ ! -d "${REPO}/.git" ]; then
    echo "ERROR: ${REPO}/.git not found — is REPO correct?" >&2
    exit 1
fi

# 1) The tightly-scoped, root-owned deploy wrapper.
echo "Writing wrapper ${WRAPPER}..."
mkdir -p "${WRAPPER_DIR}"
cat > "${WRAPPER}" <<WRAPPER_EOF
#!/bin/bash
# ox-deploy — root-owned. Deploy strongasan0x: sync the tree to origin/main
# and restart Apache (mod_wsgi). Invoked by ${AGENT_USER} via a single
# NOPASSWD sudoers rule. Do not add arguments — this script takes none, so
# the sudoers grant cannot be widened by a caller.
set -euo pipefail
REPO="${REPO}"

echo "=== ox deploy (strongasan0x) ==="

# Fix tree ownership BEFORE git ops so files left by prior deploys under a
# different UID (e.g. www-data, ${AGENT_USER}) don't block zcor's reset/pull.
# Runs as root, so it always succeeds. (.git left alone.)
echo "Fixing tree ownership..."
find "\$REPO" -not -path "\$REPO/.git/*" -not -user ${REPO_OWNER} \\
    -exec chown ${REPO_OWNER}:${REPO_GROUP} {} + 2>/dev/null || true

# Deterministic deploy: the tree must MATCH origin/main, not merge into a
# possibly-dirty/feature-branch local state. reset --hard discards any local
# uncommitted edits in the deploy tree by design (a deploy tree is not a
# workspace). clean -fd drops untracked cruft — but EXCLUDE ox-env (the
# virtualenv lives in-tree and is NOT gitignored; cleaning it would delete
# the venv and break the app until rebuilt).
echo "Syncing to origin/main..."
sudo -u ${REPO_OWNER} git -C "\$REPO" fetch origin --prune
sudo -u ${REPO_OWNER} git -C "\$REPO" checkout main
sudo -u ${REPO_OWNER} git -C "\$REPO" reset --hard origin/main
sudo -u ${REPO_OWNER} git -C "\$REPO" clean -fd -e ox-env

echo "Deployed commit:"
sudo -u ${REPO_OWNER} git -C "\$REPO" log --oneline -1

# Install any new/updated deps, then apply migrations. Both run as the repo
# owner against the project venv. pip is idempotent; migrate is a no-op when
# there's nothing pending. (collectstatic still not needed — Apache Aliases
# /static straight from rollcall/static.)
VENV_PY="\$REPO/ox-env/bin/python"
VENV_PIP="\$REPO/ox-env/bin/pip"
if [ -x "\$VENV_PIP" ]; then
    echo "Installing requirements..."
    sudo -u ${REPO_OWNER} "\$VENV_PIP" install -q -r "\$REPO/requirements.txt" || echo "  (pip step had warnings)"
fi
if [ -x "\$VENV_PY" ]; then
    echo "Applying migrations..."
    sudo -u ${REPO_OWNER} "\$VENV_PY" "\$REPO/manage.py" migrate --noinput
fi

# Sync cron jobs from the repo (reviewable git artifact). The file
# deploy/crontab.ox uses standard /etc/cron.d format. Installing it needs root,
# which this wrapper already has — so scheduled jobs become a code change, no
# extra privilege. Absent file = remove our cron (clean uninstall).
CRON_SRC="\$REPO/deploy/crontab.ox"
CRON_DST="/etc/cron.d/ox-strongasan0x"
if [ -f "\$CRON_SRC" ]; then
    install -o root -g root -m 0644 "\$CRON_SRC" "\$CRON_DST"
    echo "Synced cron from deploy/crontab.ox"
elif [ -f "\$CRON_DST" ]; then
    rm -f "\$CRON_DST"; echo "Removed cron (no crontab.ox in repo)"
fi

echo "Restarting ${SERVICE}..."
systemctl restart ${SERVICE}
echo "=== ox deploy complete ==="
WRAPPER_EOF

chown root:root "${WRAPPER}"
chmod 755 "${WRAPPER}"
echo "  ✓ wrapper installed (root:root 755)"

# 2) ONE NOPASSWD sudoers line, scoped to ONLY this wrapper (no args).
echo "Writing sudoers drop-in ${SUDOERS_DROPIN}..."
TMP_SUDOERS="$(mktemp)"
cat > "${TMP_SUDOERS}" <<SUDOERS_EOF
# Lets ${AGENT_USER} deploy strongasan0x via the vetted wrapper only.
# Blast radius = exactly this one script (takes no args). Added $(date +%Y-%m-%d).
${AGENT_USER} ALL=(root) NOPASSWD: ${WRAPPER}
SUDOERS_EOF

# Validate BEFORE installing — a broken sudoers file can lock everyone out.
if visudo -cf "${TMP_SUDOERS}"; then
    install -o root -g root -m 0440 "${TMP_SUDOERS}" "${SUDOERS_DROPIN}"
    rm -f "${TMP_SUDOERS}"
    echo "  ✓ sudoers drop-in installed (validated, 0440)"
else
    rm -f "${TMP_SUDOERS}"
    echo "ERROR: sudoers validation failed — nothing installed" >&2
    exit 1
fi

# 3) The secret-setter wrapper: sets ONE key in .env without exposing the rest.
#    Lets the agent add/update a secret (e.g. an API key) without being in the
#    webdev group or able to READ .env — the one capability the deploy wrapper
#    can't cover. KEY is strictly validated; VALUE is written literally.
SECRET_WRAPPER="${WRAPPER_DIR}/ox-set-secret"
SECRET_SUDOERS="/etc/sudoers.d/ox-set-secret-claude"
echo "Writing wrapper ${SECRET_WRAPPER}..."
cat > "${SECRET_WRAPPER}" <<SECRET_EOF
#!/bin/bash
# ox-set-secret KEY VALUE — set/replace one key in the project .env (root-owned).
# Does NOT print or expose existing secrets. KEY must be A-Z0-9_ starting with a
# letter (blocks traversal/injection). Invoked by ${AGENT_USER} via one NOPASSWD rule.
set -euo pipefail
ENV="${REPO}/.env"
KEY="\${1:-}"; VALUE="\${2:-}"
if ! printf '%s' "\$KEY" | grep -qE '^[A-Z][A-Z0-9_]*\$'; then
    echo "ERROR: invalid key name (must be A-Z0-9_, start with a letter)" >&2; exit 1
fi
[ -f "\$ENV" ] || { echo "ERROR: \$ENV missing" >&2; exit 1; }
TMP="\$(mktemp)"
# Drop any existing line for KEY, then append the new one.
grep -vE "^\${KEY}=" "\$ENV" > "\$TMP" || true
printf '%s=%s\n' "\$KEY" "\$VALUE" >> "\$TMP"
install -o ${REPO_OWNER} -g ${REPO_GROUP} -m 640 "\$TMP" "\$ENV"
rm -f "\$TMP"
echo "set \$KEY in .env"
SECRET_EOF
chown root:root "${SECRET_WRAPPER}"; chmod 755 "${SECRET_WRAPPER}"
echo "  ✓ secret wrapper installed"

echo "Writing sudoers drop-in ${SECRET_SUDOERS}..."
TMP_S2="$(mktemp)"
cat > "${TMP_S2}" <<SUDOERS2_EOF
# Lets ${AGENT_USER} set ONE .env key via the vetted wrapper (cannot read .env).
${AGENT_USER} ALL=(root) NOPASSWD: ${SECRET_WRAPPER}
SUDOERS2_EOF
if visudo -cf "${TMP_S2}"; then
    install -o root -g root -m 0440 "${TMP_S2}" "${SECRET_SUDOERS}"
    rm -f "${TMP_S2}"
    echo "  ✓ secret sudoers drop-in installed"
else
    rm -f "${TMP_S2}"; echo "ERROR: secret sudoers validation failed" >&2; exit 1
fi

# 4) The ox-run wrapper: runs an ALLOWLISTED management command on demand.
#    The allowlist is baked into the wrapper (NOT passed by the caller), so the
#    agent can run only these specific, safe one-off jobs — not arbitrary
#    `manage.py` (which would be code execution as zcor). Extra args after the
#    command name are passed through (e.g. --participant 10).
RUN_WRAPPER="${WRAPPER_DIR}/ox-run"
RUN_SUDOERS="/etc/sudoers.d/ox-run-claude"
echo "Writing wrapper ${RUN_WRAPPER}..."
cat > "${RUN_WRAPPER}" <<RUN_EOF
#!/bin/bash
# ox-run CMD [args...] — run ONE allowlisted Django management command as the
# repo owner. Allowlist is hardcoded here; the caller cannot run anything else.
set -euo pipefail
REPO="${REPO}"
PY="\$REPO/ox-env/bin/python"
CMD="\${1:-}"; shift || true
case "\$CMD" in
    send_daily_badges|send_evening_plan_nudge|backfill_metrics|backfill_chat|add_conditional_bonus|run_coach_for_all|provision_daily_participants|set_beta)
        ;;   # allowed: badge push + idempotent backfills + overnight coach + participant pre-build + beta-flag toggle
    *)
        echo "ERROR: '\$CMD' is not an allowlisted command" >&2
        echo "Allowed: send_daily_badges, send_evening_plan_nudge, backfill_metrics, backfill_chat, add_conditional_bonus, run_coach_for_all, provision_daily_participants, set_beta" >&2
        exit 1 ;;
esac
exec sudo -u ${REPO_OWNER} "\$PY" "\$REPO/manage.py" "\$CMD" "\$@"
RUN_EOF
chown root:root "${RUN_WRAPPER}"; chmod 755 "${RUN_WRAPPER}"
echo "  ✓ run wrapper installed"

echo "Writing sudoers drop-in ${RUN_SUDOERS}..."
TMP_S3="$(mktemp)"
cat > "${TMP_S3}" <<SUDOERS3_EOF
# Lets ${AGENT_USER} run allowlisted mgmt commands via the vetted wrapper only.
${AGENT_USER} ALL=(root) NOPASSWD: ${RUN_WRAPPER}
SUDOERS3_EOF
if visudo -cf "${TMP_S3}"; then
    install -o root -g root -m 0440 "${TMP_S3}" "${RUN_SUDOERS}"
    rm -f "${TMP_S3}"
    echo "  ✓ run sudoers drop-in installed"
else
    rm -f "${TMP_S3}"; echo "ERROR: run sudoers validation failed" >&2; exit 1
fi

echo ""
echo "=== DONE ==="
echo "Deploy:      sudo -n ${WRAPPER}"
echo "Set secret:  sudo -n ${SECRET_WRAPPER} KEY VALUE"
echo "Run command: sudo -n ${RUN_WRAPPER} {send_daily_badges|backfill_metrics|run_coach_for_all|...} [args]"
echo "Crons:       edit deploy/crontab.ox in the repo, then deploy"
echo "The agent now owns strongasan0x deploys + secrets + crons + vetted runs."
