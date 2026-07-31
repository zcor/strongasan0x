# Dashboard UX conventions

- Dashboard controls scroll with the page. Do not make them fixed or sticky.
- Keep one page-level scroll; do not create a separately scrollable habits section unless explicitly requested.

## Concurrent Git safety

Read `CLAUDE.md` for project rules. Other Claude, Codex, human, or automation
sessions may be active in this repository. Before editing, staging, committing,
pushing, creating a worktree, or responding to a dirty checkout, use
`$git-session-safety`.

- Never assume a dirty, staged, untracked, or registered-worktree path belongs
  to this session. Preserve unknown state and report it; do not sweep it with
  `reset`, `restore`, `clean`, `stash`, `worktree prune`, or branch deletion.
- Commit only explicitly owned paths. Do not use `git add -A`, `git add .`, or
  an unscoped commit in a shared checkout.
- Session close-out does not authorize a merge, deployment, bot action,
  publication, or external message. An integration owner re-checks the remote
  target and candidate lineage immediately before integrating.

## GitHub Actions

GitHub Actions spending is permanently `$0`. The PR-only workflow is optional
hosted evidence while it is supplied at no charge; it is not the sole merge
path. Follow `README.md`'s exact-SHA local procedure if GitHub prevents the job
from starting because of billing, quota, or runner admission. A job that
actually starts and fails remains blocking. Do not add a payment method, raise
a spending cap, buy a runner, weaken validation, or infer deployment authority
from an integration request.
