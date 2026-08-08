---
name: closeout
description: Close out a Strong as an 0x engineering or Roll Call session: preserve non-obvious context, validate the completed scope, and commit or push only agent-owned changes. Use only when the user explicitly says to close out, hand off, finalize the session, log out, or stop work. Do not use for an ordinary fix, weekly Roll Call preparation, or publication.
---

# Strong as an 0x closeout

Read and follow the [canonical closeout procedure](../../../CLOSEOUT.md). Do not duplicate it.

Start with a one-line classification and model recommendation:

- Mechanical documentation-only session: Terra Medium.
- Routine code or Roll Call closeout: Terra High.
- Live runtime state, credentials, conflicting Git state, or an ambiguous worktree lifecycle: Sol High.

Do not change the active model yourself. Do not pause solely to downgrade a capable model.

An explicit closeout request authorizes only the procedure's scoped repository actions for the
current session. It never authorizes a new Roll Call publication, social post, Telegram or Discord
message, data deletion, configuration substitution, scheduler change, or work in another project.

Report the delivered state, validation evidence, commit and push state, handoff decision, retained
or released worktrees, remaining work, and explicit non-actions.
