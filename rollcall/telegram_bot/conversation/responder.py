"""Conversational responder — spawns Claude CLI as subprocess.

Per plan binary-juggling-locket.md → Claude CLI invocation layer.

  claude -p "<prompt>" \\
    --output-format stream-json \\
    --allowedTools "Bash(python manage.py bot_query:*)" \\
    --max-turns 8

Concurrency:
  - global asyncio.Semaphore(2) caps simultaneous Claude calls
  - per-chat asyncio.Lock prevents two replies racing in one chat
  - 8s per-chat cooldown after a reply (in messages.py wiring)

Failure handling:
  - circuit breaker opens on quota errors (6h cooldown, persisted to disk)
  - timeout (90s default) → graceful in-character "the watch falls quiet" reply

Phase B is DM-only. Phase D unlocks groups.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

from rollcall.telegram_bot.conversation.circuit_breaker import (
    claude_is_available,
    looks_like_quota_error,
    mark_claude_unavailable,
)
from rollcall.telegram_bot.conversation.persona import BOT_PERSONA

logger = logging.getLogger(__name__)


# Global semaphore — created lazily on first use to bind to the running loop.
_global_sema: Optional[asyncio.Semaphore] = None


def _get_global_sema() -> asyncio.Semaphore:
    global _global_sema
    if _global_sema is None:
        n = getattr(settings, "CONVERSATION_GLOBAL_CONCURRENCY", 2)
        _global_sema = asyncio.Semaphore(n)
    return _global_sema


@dataclass
class ReplyContext:
    user_text: str
    chat_id: int
    chat_type: str  # "private" | "supergroup" | etc
    viewer_telegram_id: int
    viewer_display: str
    viewer_warrior: Optional[str]
    intent: str  # from classifier
    target_warrior: Optional[str]
    recent_history: list = field(default_factory=list)


@dataclass
class ReplyResult:
    text: Optional[str]
    error: Optional[str] = None
    latency_ms: int = 0


VERB_COOKBOOK = """You have one tool: Bash(python manage.py bot_query:*).
Each call returns a JSON envelope: {"ok": true, "data": ..., "rows": N} or {"ok": false, "error": "..."}.

Available verbs (always callable):
  warrior_history --json '{"name": "<canonical>", "weeks": 12}'
    → last N weeks of attestations + extracted metrics for one warrior
  warrior_metric_series --json '{"name": "<canonical>", "metric": "bench_press", "weeks": 52}'
    → time series of one metric. Allowed metrics: bench_press, squat, deadlift,
      daily_steps, calories_burned, sleep_hours, body_weight, body_fat_pct,
      strength_sessions, cardio_sessions, combat_sessions, total_training_sessions,
      protein_grams, calories_consumed, resting_heart_rate, vo2_max
  weekly_leaderboard --json '{"week_end": "YYYY-MM-DD"}'
    → published RollCallRanking for that week
  compare_warriors --json '{"names": ["A", "B"], "metric": "bench_press", "weeks": 12}'
    → side-by-side metric series
  seasonal_breakdown --json '{"name": "<canonical>", "metric": "bench_press"}'
    → mean/stdev/min/max per season (winter, spring, summer, fall)
  recent_attestations --json '{"name": "<canonical>", "n": 4}'
    → last N raw attestation texts (truncated to 1500 chars each)

DM-only (returns "verb not available in this scope" otherwise):
  list_my_uploads --json '{}'  — viewer's own private upload metadata, no bytes
  read_my_upload --json '{"upload_id": N}' — bytes of one of the viewer's uploads

Use the verbs to gather data, then write the reply yourself. Do not paste raw
JSON in your final answer — synthesize the numbers into prose."""


def _build_responder_prompt(ctx: ReplyContext, scope: str) -> str:
    history_block = ""
    if ctx.recent_history:
        lines = []
        for turn in ctx.recent_history[-12:]:
            role = turn.get("role", "warrior")
            name = turn.get("name", "?")
            t = (turn.get("text") or "").replace("\n", " ").strip()
            if len(t) > 200:
                t = t[:200] + "…"
            lines.append(f"  [{role}] {name}: {t}")
        history_block = "\n\nRecent chat history (oldest to newest):\n" + "\n".join(lines)

    target_block = ""
    if ctx.target_warrior:
        target_block = f"\nThe warrior is asking about: {ctx.target_warrior}"

    return f"""{BOT_PERSONA}

You are responding to a message in a Telegram {ctx.chat_type} chat.
Viewer: {ctx.viewer_display}{f" (warrior: {ctx.viewer_warrior})" if ctx.viewer_warrior else " (not linked to any warrior)"}
Classifier intent: {ctx.intent}{target_block}

Scope rules (CRITICAL):
- chat_type is "{ctx.chat_type}", scope is "{scope}".
- In group chats you CANNOT see any warrior's private uploads (the verbs are not available).
- In DMs you can ONLY see the viewer's OWN private uploads. Asking about another warrior's uploads → refuse in voice ("That ledger is sealed to its keeper.")
- All other warrior data (attestations, rankings, extracted metrics) is public and shareable.

{VERB_COOKBOOK}{history_block}

The warrior just said:
---
{ctx.user_text}
---

Investigate using verbs as needed (call them via Bash), then reply in voice. Keep it short unless they explicitly asked for depth. Do NOT prefix your reply with "Here's what I found" or similar — just answer."""


async def generate_reply(ctx: ReplyContext) -> ReplyResult:
    """Spawn Claude CLI for a single reply. Returns the assistant text."""
    if not claude_is_available():
        logger.info("Claude CLI circuit is OPEN; returning fallback")
        return ReplyResult(
            text="The watch falls quiet for now — the oracle's bandwidth is depleted. Try me again in a few hours.",
            error="circuit_open",
        )

    cli_path = getattr(settings, "CONVERSATION_CLAUDE_CLI_PATH", "claude")
    timeout_sec = getattr(settings, "CONVERSATION_CLAUDE_TIMEOUT_SEC", 90)
    scope = "dm" if ctx.chat_type == "private" else "group"

    prompt = _build_responder_prompt(ctx, scope)

    # Env for the subprocess: viewer ID, scope, plus PATH augmented with
    # common homebrew/local-bin locations so the Claude CLI (Node.js script)
    # can find `node`. launchd's PATH is minimal and won't include /opt/homebrew.
    env = os.environ.copy()
    env["VIEWER_TELEGRAM_ID"] = str(ctx.viewer_telegram_id)
    env["BOT_SCOPE"] = scope
    extra_paths = ["/opt/homebrew/bin", "/usr/local/bin"]
    current_path = env.get("PATH", "")
    for p in extra_paths:
        if p not in current_path:
            current_path = f"{p}:{current_path}" if current_path else p
    env["PATH"] = current_path

    cmd = [
        cli_path,
        "-p", prompt,
        "--allowedTools", "Bash(python manage.py bot_query:*)",
        "--max-turns", "8",
        "--output-format", "json",  # final JSON envelope at end
    ]

    logger.info("responder: spawning claude (chat=%s viewer=%s scope=%s)", ctx.chat_id, ctx.viewer_telegram_id, scope)
    started = time.monotonic()

    sema = _get_global_sema()
    async with sema:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(settings.BASE_DIR),
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("responder: timeout after %ds; killing", timeout_sec)
                proc.kill()
                await proc.wait()
                return ReplyResult(
                    text="The reckoning takes longer than the watch allows. Try a narrower question.",
                    error="timeout",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
        except FileNotFoundError:
            logger.error("responder: claude CLI not found at %s", cli_path)
            return ReplyResult(
                text=None,
                error=f"claude_cli_not_found: {cli_path}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            logger.exception("responder: subprocess failed")
            return ReplyResult(text=None, error="subprocess_error",
                               latency_ms=int((time.monotonic() - started) * 1000))

    latency_ms = int((time.monotonic() - started) * 1000)
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    if looks_like_quota_error(stderr) or looks_like_quota_error(stdout):
        logger.warning("responder: quota error detected, opening circuit. stderr=%s", stderr[:300])
        mark_claude_unavailable()
        return ReplyResult(
            text="The watch falls quiet — the oracle's bandwidth is depleted. Try me again in a few hours.",
            error="quota",
            latency_ms=latency_ms,
        )

    if proc.returncode != 0:
        # Claude CLI with --output-format json writes a JSON envelope to stdout
        # even on auth/quota failures (with is_error=true, api_error_status=N).
        # Parse it so we can distinguish 401-auth (fixable) from 5xx (transient)
        # from real exceptions.
        cli_status, cli_msg = _parse_cli_error(stdout)
        logger.warning(
            "responder: claude exited %s. cli_status=%s cli_msg=%r stderr=%r",
            proc.returncode, cli_status, (cli_msg or "")[:300], stderr[:200],
        )
        if cli_status == 401:
            user_text = "The watch is unauthenticated — the oracle does not know me. The keeper must run `claude login` on the host."
        elif cli_status in (429, 529):
            mark_claude_unavailable()
            user_text = "The watch falls quiet — the oracle's bandwidth is depleted. Try me again in a few hours."
        else:
            user_text = "The watch tried to consult the oracle and was rebuffed."
        return ReplyResult(
            text=user_text,
            error=f"exit_{proc.returncode} cli_status={cli_status}",
            latency_ms=latency_ms,
        )

    # Claude CLI's --output-format json emits a final JSON object with .result
    text = _extract_final_text(stdout)
    if not text:
        logger.warning("responder: empty extract from stdout (%d chars): %r", len(stdout), stdout[:300])
        return ReplyResult(
            text="The oracle answered, but the watch could not parse the words.",
            error="empty_output",
            latency_ms=latency_ms,
        )

    logger.info("responder: ok latency=%dms chars=%d", latency_ms, len(text))
    return ReplyResult(text=text, latency_ms=latency_ms)


def _parse_cli_error(stdout: str) -> tuple:
    """Parse a Claude CLI JSON envelope for an api error. Returns (status, message)."""
    import json as _json
    stdout = (stdout or "").strip()
    if not stdout:
        return None, None
    try:
        obj = _json.loads(stdout)
        if isinstance(obj, dict) and obj.get("is_error"):
            return obj.get("api_error_status"), obj.get("result")
    except _json.JSONDecodeError:
        pass
    return None, None


def _extract_final_text(stdout: str) -> Optional[str]:
    """Pull the assistant's final reply from claude CLI --output-format json output.

    Format depends on CLI version. Try the json envelope first; fall back to
    last non-empty line if parsing fails.
    """
    import json as _json
    stdout = stdout.strip()
    if not stdout:
        return None
    # Try to parse as a single JSON object first (--output-format json)
    try:
        obj = _json.loads(stdout)
        # Common shapes: {"result": "..."} or {"messages": [{"content": "..."}]}
        if isinstance(obj, dict):
            if "result" in obj and isinstance(obj["result"], str):
                return obj["result"].strip() or None
            if "messages" in obj and isinstance(obj["messages"], list):
                for m in reversed(obj["messages"]):
                    if isinstance(m, dict):
                        c = m.get("content")
                        if isinstance(c, str) and c.strip():
                            return c.strip()
                        if isinstance(c, list):
                            for blk in c:
                                if isinstance(blk, dict) and blk.get("type") == "text":
                                    t = (blk.get("text") or "").strip()
                                    if t:
                                        return t
    except _json.JSONDecodeError:
        pass
    # Fallback: maybe stream-json (one JSON per line); take the last "result"-bearing line
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = _json.loads(line)
            if isinstance(obj, dict) and isinstance(obj.get("result"), str):
                return obj["result"].strip() or None
        except _json.JSONDecodeError:
            continue
    # Last resort: return the raw stdout (some CLI versions emit plain text)
    return stdout if stdout else None
