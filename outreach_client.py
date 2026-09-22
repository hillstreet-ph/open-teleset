"""Consent checks at the Telegram RPC boundary, after uploads and resolution.

Only the central outreach dispatcher establishes the trusted context. A context
cannot authorize another client, recipient, action category, or native schedule.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from telethon import TelegramClient, functions, types, utils
from telethon.tl.tlobject import TLRequest


@dataclass(frozen=True)
class OutreachContext:
    action: str
    subject: object
    account_id: str
    approval_id: str | None
    delay: float | None = None


_current_outreach: ContextVar[OutreachContext | None] = ContextVar("outreach_context", default=None)


@contextmanager
def outreach_context(*, action, subject, account_id, approval_id, delay=None):
    """Bind a trusted dispatcher decision to this async operation only."""
    token = _current_outreach.set(OutreachContext(action, subject, account_id, approval_id, delay))
    try:
        yield
    finally:
        _current_outreach.reset(token)


def _inner_request(request):
    # Native invocation wrappers must not hide a messaging request from the gate.
    while isinstance(getattr(request, "query", None), TLRequest):
        request = request.query
    return request


def _category(request):
    if isinstance(request, (
        functions.messages.CreateChatRequest,
        functions.messages.AddChatUserRequest,
        functions.channels.InviteToChannelRequest,
    )):
        return "member_add"
    name = type(request).__name__
    if type(request).__module__ == "telethon.tl.functions.messages" and (
        name.startswith("Send") or name in {"ForwardMessagesRequest", "EditMessageRequest", "EditInlineBotMessageRequest"}
    ):
        return "message"
    return None


def _recipient(request, category):
    from outreach_policy import OutreachDenied

    if category == "member_add":
        users = getattr(request, "users", None)
        if users is not None:
            if len(users) != 1:
                raise OutreachDenied("one_member_per_rpc_required")
            return users[0]
        return request.user_id
    if isinstance(request, functions.messages.ForwardMessagesRequest):
        return request.to_peer
    peer = getattr(request, "peer", None)
    if peer is None:
        # Inline/encrypted variants without a verifiable recipient are unsupported.
        raise OutreachDenied("unverifiable_rpc_recipient")
    return peer


def _validate_shape(request):
    from outreach_policy import OutreachDenied

    if getattr(request, "schedule_date", None) is not None or getattr(request, "schedule_repeat_period", None):
        raise OutreachDenied("native_schedule_cannot_revalidate")
    if isinstance(request, functions.messages.SendMultiMediaRequest):
        if not 1 <= len(request.multi_media) <= 10:
            raise OutreachDenied("album_rpc_bounds_exceeded")


class ConsentTelegramClient(TelegramClient):
    """Require fresh approval for each outbound RPC, including album chunks."""

    def __init__(self, *args, outreach_account_id=None, **kwargs):
        kwargs["request_retries"] = 0
        kwargs["flood_sleep_threshold"] = 0
        kwargs["raise_last_call_error"] = True
        super().__init__(*args, **kwargs)
        self.outreach_account_id = outreach_account_id

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        from outreach_policy import OutreachDenied, dispatch_outreach

        # Force no automatic retry/sleep even if a caller changes client settings.
        self._request_retries = 0
        self.flood_sleep_threshold = 0
        if utils.is_list_like(request):
            results = []
            for item in request:
                results.append(await self._call(sender, item, ordered=ordered))
            return results

        inner = _inner_request(request)
        category = _category(inner)
        if category is None:
            return await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=0)
        _validate_shape(inner)
        context = _current_outreach.get()
        peer = _recipient(inner, category)
        if context is None:
            if category == "message" and isinstance(peer, types.InputPeerSelf):
                return await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=0)
            raise OutreachDenied("outreach_context_required")
        if not self.outreach_account_id or context.account_id != self.outreach_account_id:
            raise OutreachDenied("outreach_account_mismatch")
        if (category == "member_add" and context.action != "member_add") or (
            category == "message" and context.action not in {"personal_send", "batch_send", "scheduled_send"}
        ):
            raise OutreachDenied("outreach_action_mismatch")

        # Resolution may await read-only RPCs. Consent is checked after it finishes.
        await inner.resolve(self, utils)
        resolved_peer = _recipient(inner, category)
        self_id = None
        if isinstance(resolved_peer, types.InputPeerSelf):
            me = await self.get_me()
            self_id = me.id

        def verify_recipient():
            if _inner_request(request) is not inner:
                raise OutreachDenied("outreach_request_changed")
            _validate_shape(inner)
            current_peer = _recipient(inner, category)
            try:
                subject = self_id if isinstance(current_peer, types.InputPeerSelf) else utils.get_peer_id(current_peer)
            except (TypeError, ValueError):
                raise OutreachDenied("unverifiable_rpc_recipient") from None
            if subject is None or str(subject) != str(context.subject):
                raise OutreachDenied("outreach_recipient_mismatch")

        verify_recipient()

        async def send_native():
            # Recheck mutable request fields after any pacing wait.
            verify_recipient()
            return await super(ConsentTelegramClient, self)._call(
                sender, request, ordered=ordered, flood_sleep_threshold=0
            )

        return await dispatch_outreach(
            action=context.action, subject=context.subject, account_id=context.account_id,
            approval_id=context.approval_id, delay=context.delay, send=send_native,
        )
