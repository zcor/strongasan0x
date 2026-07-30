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
