# Roll Call handoff

**Date:** 2026-08-01

**Status:** The Roll Call for July 20-26, 2026 is live. The direct-site publication repair and this record are on `codex/roll-call-web-cleanup-20260801`. They are not merged into `feat/roll-call-skills` or deployed as code.

## What changed this session

- Published [the Roll Call](https://strongasan0x.com/roll-call/2026-07-26/) with the editorial order RektDiomedes, CurveCap, Battman. Public verification returned HTTP 200.
- Published the [ranking post](https://x.com/StrongAsAn0x/status/2083470742742765736) and [reply](https://x.com/StrongAsAn0x/status/2083470996544299450). Telegram messages 2214 and 2215 and the Discord `#🎖️︲rankings` post were also sent.
- Replaced factual errors in the generated ode before publication. The raw attestations remain unchanged. `no-ai-slop` passed on the saved ode and post markdown.
- The AI table after 13 DeepSeek trials was RektDiomedes 1.54 plus or minus 0.22, CurveCap 1.69 plus or minus 0.13, and Battman 2.77 plus or minus 0.17. The user selected the first two positions after reviewing the text.
- This branch's prior commit `cda4180` removes the retired Substack route from the Roll Call workflow. Its focused publication URL tests passed before this closeout.

## Live facts and unresolved items

- The page is database-driven. No application deployment was required for this week's publication.
- CurveCap's Garmin export still reports Sunday-Saturday, July 19-25, rather than the contest's Monday-Sunday window. It also omits weights. The ode avoided claiming the export's out-of-window aggregate step and calorie totals; the raw attestation was published unchanged.
- RektDiomedes and Battman have no Discord user mapping, so the Top 10 role command could not assign them. CurveCap, Spencer420, and Rktbay already hold the role. `DISCORD_TOP_10_CHANNEL_ID` is unset, so channel permissions were not changed.

## Start here next time

- If integrating the direct-site cleanup, review and merge `cda4180` and the closeout commit into a named target branch. Re-run `python manage.py test rollcall.tests.test_publication_urls --keepdb` before deployment.
- Add Discord mappings for RektDiomedes and Battman if they want Top 10 role assignment.

Memory: [[strongasan0x-roll-call-publication]]
