# Close-out procedure

Use this procedure only when the user explicitly says to close out, hand off, finalize, log out,
or stop the Strong as an 0x session. It preserves useful context without turning every small fix
into a ceremony.

## 1. Classify and take stock

State the complexity classification before editing. Inspect the current checkout, staged state,
agent-owned worktrees, relevant live service state, and the outcome the user asked for. Never
assume an inherited shell credential, a desktop checkout, or a stale status file represents the
Mini's Strong as an 0x runtime.

If the shared checkout is dirty and those changes are not clearly yours, leave it alone. Use an
agent-owned clean worktree for closeout artifacts and record that lifecycle in the handoff.

## 2. Validate only what changed

Run the smallest evidence-producing checks for the completed scope. For Roll Call syndication,
use the Mini wrapper and the no-send preflight after the public page is live. Dry runs are safe;
do not repost a completed week merely to prove a command works. For a persistent bot change,
verify its launchd state and process identity rather than assuming a matching process is durable.

## 3. Preserve durable context deliberately

Write a handoff only when another session would otherwise need to reconstruct non-obvious state:
an unfinished diagnosis, an unlanded worktree, a changed theory, a pending deployment, or an
unresolved decision. Use [handoffs/README.md](handoffs/README.md) and a specific dated filename.

Skip the handoff for a trivial, fully closed task. Do not put credentials, health attestations,
chat content, or other private data in a tracked handoff. Put enduring operating rules in
`CLAUDE.md` or the relevant skill, not in a one-off handoff.

## 4. Commit and push without absorbing other work

Commit only files you own from an agent-owned clean worktree. Before and after the commit, inspect
both the working tree and the commit's file list. Use the repository's Git-session safety guidance
when it is available. Never use `git add -A`, `git add .`, a broad pathspec, `git reset`, or a
force push in a shared checkout.

Use explicit paths. For example:

```sh
git add -- path/to/owned-file path/to/another-owned-file
git commit --only -m "docs: record Roll Call closeout" -- path/to/owned-file path/to/another-owned-file
git show --stat --oneline HEAD
```

An explicit closeout request authorizes pushing the verified, agent-owned commit to the canonical
repository branch. If a normal push is rejected, fetch and rebase only from the clean owned
worktree, resolve only this session's changes, then retry. Never force-push. If the request did
not explicitly ask to close out, report the ready commit instead of pushing.

## 5. Release only owned clean worktrees

When an agent-owned worktree is clean and its source is landed, record its path, branch or commit,
tip, and release decision in the handoff. Remove it only when it is clearly agent-owned; retain its
branch or recovery ref. A clean worktree is not permission to remove a user-owned one.

## 6. Report and stop

State what is live, what source was committed and pushed, the checks that passed, what was not
done, and any real loose end. Closeout never implies a reboot, cleanup sweep, data deletion,
credential rotation, publication, messaging, or unrelated scheduler action.
