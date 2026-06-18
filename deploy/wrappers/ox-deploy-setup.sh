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

# No migrations/collectstatic here: this app has none of consequence and
# serves /static directly from rollcall/static via Apache Alias. If that
# changes, add the steps here (run as ${REPO_OWNER}).

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

echo ""
echo "=== DONE ==="
echo "Verify (as ${AGENT_USER}):  sudo -n ${WRAPPER}"
echo "The agent now owns strongasan0x deploys unattended."
