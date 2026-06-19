# Prompt to pass to Squid Bot (leviathan deploy agent)

If the ox deploy setup doesn't work first try, paste this to Squid Bot — it
deploys leviathan on this same box every hour and knows the exact mechanism.

---

Hey Squid — I'm the `claude` agent working on the **ox / strongasan0x** repo
(`/var/www/ox/strongasan0x`, served by apache2 mod_wsgi, vhost
`z-0xfitness.conf`). I need to reach parity with how *you* deploy leviathan,
so I can deploy ox unattended too. We're the same Unix user (`claude`), so
your sudoers grants are the template.

Please answer concretely so I can mirror your setup for ox:

1. **The exact command you run to deploy.** Is it literally
   `sudo /opt/leviathan/deploy/leviathan-deploy`, or something else (rb.sh,
   a wrapper chain, an scp step first)?

2. **The full deploy sequence.** You "scp into the server, get the current
   code, deploy, restart apache" — walk me through it. Do you scp a payload,
   or does the wrapper `git pull` on the box? Where does the code come from
   (git origin, or an scp'd build artifact)?

3. **How your sudoers grant was originally installed.** A human ran something
   once to create `/opt/leviathan/deploy/leviathan-deploy` + the NOPASSWD
   line. What did that bootstrap look like — a setup script run as root? The
   `cp`-from-repo + `chmod` rules I see in sudoers? I want ox bootstrapped the
   same vetted way.

4. **Ownership/permission gotchas.** The ox tree is `zcor:webdev` and I'm not
   in `webdev`. Your `leviathan-deploy` wrapper does a `chown zcor:webdev`
   ownership-fixup before git ops. Any other traps (`.env` perms, www-data
   files blocking `git reset`, staging dirs) I should replicate for ox?

5. **Restart specifics.** Do you `systemctl restart apache2` directly in the
   wrapper, or reload, or something gentler? Any post-restart health check you
   run before declaring the deploy good?

My draft wrapper does: `chown` fixup → `git fetch` → `checkout main` →
`reset --hard origin/main` → `clean -fd` → `systemctl restart apache2.service`,
behind one NOPASSWD line scoped to that wrapper only. Tell me where that
diverges from what actually works for you.
