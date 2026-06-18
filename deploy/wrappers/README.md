# Deploy wrappers

## `ox-deploy-setup.sh` — one-time agent deploy access

Lets the `claude` agent account deploy strongasan0x unattended, with the
tightest possible blast radius. Mirrors the `leviathan-deploy` install
pattern already trusted on this box.

**Why this exists:** the deploy tree `/var/www/ox/strongasan0x` is owned by
`zcor:webdev`. The `claude` agent is a separate, lower-privilege user (not in
`webdev`), so it can't `git pull` the tree, and `sudo -u zcor git` prompts for
a password it doesn't have. This is an OS-level wall — the agent cannot and
should not route around it. The fix is a one-time, **root-applied** grant.

### What it installs

1. A root-owned wrapper `/opt/ox/deploy/ox-deploy` that does **only**:
   sync the tree to `origin/main` (`fetch` → `checkout main` →
   `reset --hard origin/main` → `clean -fd`) and `systemctl restart apache2`.
   It takes **no arguments**, so the grant can't be widened by the caller.
2. One NOPASSWD sudoers line, scoped to **only** that wrapper:
   `claude ALL=(root) NOPASSWD: /opt/ox/deploy/ox-deploy`
   (validated with `visudo -cf` before install).

Blast radius after this: the agent can run exactly one vetted action —
deploy strongasan0x — and nothing else new.

### Run it once (root)

```bash
sudo bash /var/www/ox/strongasan0x/deploy/wrappers/ox-deploy-setup.sh
```

(The file is already in the repo, so once `main` is on the box you can run it
straight from there — no scp needed.)

### Verify

```bash
sudo -n /opt/ox/deploy/ox-deploy        # runs a deploy
curl -s -o /dev/null -w '%{http_code}\n' https://strongasan0x.com/daily/manifest.webmanifest   # want 200
```

### Note

`ox-deploy` does `reset --hard origin/main` — the deploy tree is made to
**match** `main`, so any local uncommitted edits in the tree are discarded by
design (a deploy tree is not a workspace). Whatever is on `main` becomes prod
on each run.
