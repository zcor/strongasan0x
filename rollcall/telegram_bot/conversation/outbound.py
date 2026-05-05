"""Single outbound bot-message helper.

Every bot-originated Telegram message — command replies, attestation
confirmations, conversational replies, error messages, /private_files
listings — goes through `send_and_log`. After Phase 0, no other code
calls `bot.send_message` or `message.reply_text` directly. That keeps
`MessageLog` a faithful record of both sides of every conversation,
which the Phase A classifier and Phase B responder need.

Failure to log is non-fatal: the message still goes out. We don't want
to suppress a user-visible reply because the audit row failed to write.
"""
import logging
from typing import Optional

from rollcall.bot_commands.log_message import log_message

logger = logging.getLogger(__name__)


KIND_REPLY = "reply"                       # generic conversational reply
KIND_ATTESTATION_ACK = "attestation_ack"   # confirmation after storing an attestation
KIND_COMMAND_REPLY = "command_reply"       # response to a /command
KIND_ERROR = "error"                       # error/fallback message
KIND_PRIVATE_FILES_LISTING = "private_files_listing"
KIND_FORGET_ACK = "forget_ack"

VALID_KINDS = frozenset({
    KIND_REPLY, KIND_ATTESTATION_ACK, KIND_COMMAND_REPLY, KIND_ERROR,
    KIND_PRIVATE_FILES_LISTING, KIND_FORGET_ACK,
})


async def send_and_log(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: Optional[int] = None,
    parse_mode: Optional[str] = None,
    kind: str = KIND_REPLY,
):
    """Send a Telegram message and write a corresponding MessageLog row.

    Returns the PTB Message object on success, None on send failure.
    The MessageLog write happens after a successful send and is best-effort
    (logged but not raised) — we don't want a DB hiccup to suppress the
    user-visible reply.
    """
    if kind not in VALID_KINDS:
        logger.warning("send_and_log called with unknown kind=%r; defaulting to %r", kind, KIND_REPLY)
        kind = KIND_REPLY

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
        )
    except Exception:
        logger.exception("send_and_log: send_message failed for chat %s kind=%s", chat_id, kind)
        return None

    try:
        await log_message(
            source="telegram",
            user_mapping=None,            # bot-authored, no warrior mapping
            message_id=sent.message_id,
            chat_id=chat_id,
            content=text,
            posted_at=sent.date,
            has_attachments=False,
            attachment_count=0,
            attachment_info=None,
            is_bot_reply=True,
            kind=kind,
            classifier_verdict=None,
        )
    except Exception:
        logger.exception("send_and_log: log_message failed for sent msg %s in chat %s", sent.message_id, chat_id)

    return sent
