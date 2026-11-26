from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from app.inbound.ports import (
    ConsumerDispatchRequest,
    ConsumerDispatchResult,
    PlutusDispatchRequest,
    PlutusDispatchResult,
)


@dataclass(frozen=True)
class RecordedEmail:
    source: str
    recipients: list[str]
    subject: str
    text_body: str
    html_body: str
    reply_to: Optional[list[str]] = None


class InMemorySesAdapter:
    """In-memory SesMailPort fake recording sent emails and optional injected failures."""

    def __init__(self, *, error: Optional[Exception] = None) -> None:
        self.sent: list[RecordedEmail] = []
        self.error = error

    def send_email(
        self,
        *,
        source: str,
        recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str,
        reply_to: Optional[list[str]] = None,
    ) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(
            RecordedEmail(
                source=source,
                recipients=list(recipients),
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                reply_to=list(reply_to) if reply_to is not None else None,
            )
        )
        return f"msg-{len(self.sent)}"


@dataclass(frozen=True)
class RecordedJevCall:
    state_text: str
    questions: Dict[str, Any]


class InMemoryJevClient:
    """In-memory JevClientPort fake recording calls and returning deterministic JSON verdicts."""

    def __init__(
        self,
        *,
        responses: Optional[Sequence[Dict[str, Any]]] = None,
        default_response: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
        handler: Optional[Callable[[str, Mapping[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.calls: list[RecordedJevCall] = []
        self._responses = list(responses) if responses is not None else []
        self._default_response = (
            dict(default_response)
            if default_response is not None
            else {
                "urgency_score": 8,
                "is_transient_noise": False,
                "destination_channel": "alerts",
                "_confidence": 0.92,
            }
        )
        self.error = error
        self._handler = handler

    async def evaluate_questions(
        self,
        state_text: str,
        questions: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self.calls.append(RecordedJevCall(state_text=state_text, questions=dict(questions)))
        if self.error is not None:
            raise self.error
        if self._handler is not None:
            return dict(self._handler(state_text, questions))
        if self._responses:
            return dict(self._responses.pop(0))
        return dict(self._default_response)


@dataclass(frozen=True)
class RecordedDiscordPost:
    bot_token: str
    channel_id: str
    thread_name: str
    message_payload: Dict[str, Any]
    thread_id: Optional[str]


class InMemoryDiscordTransport:
    """In-memory DiscordTransportPort fake recording posts and simulating Forum/Text threads."""

    def __init__(
        self,
        *,
        forum_threads: bool = True,
        stale_thread_ids: Optional[set[str]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.posts: list[RecordedDiscordPost] = []
        self.forum_threads = forum_threads
        self.stale_thread_ids: set[str] = set(stale_thread_ids or ())
        self.error = error
        self._thread_seq = 0

    async def post_message(
        self,
        *,
        bot_token: str,
        channel_id: str,
        thread_name: str,
        message_payload: Dict[str, Any],
        thread_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        if self.error is not None:
            raise self.error
        self.posts.append(
            RecordedDiscordPost(
                bot_token=bot_token,
                channel_id=channel_id,
                thread_name=thread_name,
                message_payload=dict(message_payload),
                thread_id=thread_id,
            )
        )
        if thread_id is not None and thread_id not in self.stale_thread_ids:
            return {"id": f"msg-{len(self.posts)}", "channel_id": thread_id}, thread_id
        if self.forum_threads:
            self._thread_seq += 1
            created_id = f"thread-{self._thread_seq}"
            return {"id": created_id}, created_id
        return {"id": f"msg-{len(self.posts)}", "channel_id": channel_id}, None


class FakeClock:
    """Deterministic ClockPort fake for testing cooldown expiry and embed timestamps."""

    def __init__(
        self,
        initial_seconds: float = 1000.0,
        initial_utc: Optional[datetime] = None,
    ) -> None:
        self._seconds = initial_seconds
        self._utc = initial_utc or datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)

    def now_seconds(self) -> float:
        return self._seconds

    def now_utc(self) -> datetime:
        return self._utc

    def advance(self, seconds: float) -> None:
        self._seconds += seconds
        self._utc = self._utc + timedelta(seconds=seconds)


class InMemoryConsumerDispatcher:
    """In-memory ConsumerDispatchPort fake recording requests and returning typed outcomes."""

    def __init__(
        self,
        *,
        responses: Optional[Sequence[ConsumerDispatchResult]] = None,
        default_result: Optional[ConsumerDispatchResult] = None,
        handler: Optional[Callable[[ConsumerDispatchRequest], ConsumerDispatchResult]] = None,
    ) -> None:
        self.requests: list[ConsumerDispatchRequest] = []
        self._responses = list(responses) if responses is not None else []
        self._default_result = default_result or ConsumerDispatchResult(
            status="dispatched",
            consumer_ref="plutus-tx-1",
        )
        self._handler = handler

    async def dispatch_email(self, request: ConsumerDispatchRequest) -> ConsumerDispatchResult:
        self.requests.append(request)
        if self._handler is not None:
            return self._handler(request)
        if self._responses:
            return self._responses.pop(0)
        return self._default_result


InMemoryPlutusDispatcher = InMemoryConsumerDispatcher

