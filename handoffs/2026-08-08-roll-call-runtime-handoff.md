# Handoff: Roll Call runtime closeout

**Date:** 2026-08-08
**Status:** The Mini's Roll Call runtime has been corrected and verified without reposting the
week. The durable Telegram service is back under launchd, and the source configuration now has a
no-send preflight for future syndication.

## What changed this session

- Corrected the project-owned runtime configuration and dependencies on the Mini.
- Added preflight and dry-run coverage for the public Roll Call URL, social credentials, Telegram
  group access, and winner-image rendering.
- Restored the persistent Telegram bot to its direct launchd invocation and verified KeepAlive.
- Corrected workflow documentation and added the explicit closeout procedure.

## Proven state

- The published 2026-08-02 Roll Call passed the no-send preflight and both social dry runs.
- The Telegram bot is running as a launchd service, not merely as a supervised shell process.
- Runtime remediation is `b2b3d8c` and the closeout procedure is `be8d824`, both pushed to
  `origin/main` after a normal rebase.

## Start here next time

For a new week, use `roll-call-prep`; after the page is publicly live, use
`roll-call-publish` and run its required no-send preflight before any social command.

## Loose ends

- None in the live Roll Call runtime. Do not use this handoff to alter an unrelated dirty checkout.

## Worktree record

- The agent-owned worktree at `/private/tmp/strongasan0x-roll-call-runtime-20260808` used branch
  `codex/roll-call-runtime-20260808`; its retained recovery point before this final source sync is
  `14aafd7`. Its runtime and documentation content is now landed on `origin/main`.
