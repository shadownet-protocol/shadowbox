from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


Scope = Literal["global", "contact", "context"]
Direction = Literal["inbound", "outbound"]
InboundStatus = Literal["inbox", "stranger_review"]
OutboundStatus = Literal["sending", "accepted", "failed"]
SendStatus = Literal["accepted", "rejected", "sending"]
Priority = Literal["low", "normal", "high"]


class Body(Wire):
    text: str | None = None
    intent: str | None = None
    data: dict[str, Any] | None = None


class Credential(Wire):
    kind: str
    issuer: str
    org: str | None = None
    expires_at: str


class ContactProfile(Wire):
    notes: str | None = None
    priority: Priority = "normal"
    tags: list[str] = []
    expires_at: str | None = None
    data: dict[str, Any] | None = None


class DirectiveItem(Wire):
    text: str
    expires_at: str | None = None


class DirectiveLayer(Wire):
    scope: Scope
    ref: str | None = None
    items: list[DirectiveItem]
    updated_at: str


class IdentityResult(Wire):
    shadowname: str | None = None
    direct_uri: str | None = None
    pk: str
    credentials: list[Credential] = []


class ResolveInput(Wire):
    name: str


class ResolveResult(Wire):
    shadowname: str
    pk: str
    endpoint: str


class ContactsInput(Wire):
    query: str | None = None


class ContactSummary(Wire):
    shadowname: str
    display_name: str | None = None
    grants: list[str]
    last_seen: str | None = None


class ContactsResult(Wire):
    contacts: list[ContactSummary]


class ContactDetailInput(Wire):
    name: str


class ContactDetail(Wire):
    shadowname: str
    display_name: str | None = None
    pk: str
    endpoint: str
    grants: list[str]
    credentials: list[Credential] = []
    profile: ContactProfile | None = None
    added_at: str
    last_seen: str | None = None
    tls_pin: str | None = None


class AddContactInput(Wire):
    name: str
    display_name: str | None = None
    grants: list[str] = ["messaging"]
    profile: ContactProfile | None = None


class TrustWarning(Wire):
    untrusted_issuers: list[str]


class AddContactResult(Wire):
    shadowname: str
    trust_warning: TrustWarning | None = None


class GrantInput(Wire):
    name: str
    grant: str
    allowed: bool


class SetContactProfileInput(Wire):
    name: str
    profile: ContactProfile


class Ok(Wire):
    ok: Literal[True] = True


class DirectivesInput(Wire):
    contact: str | None = None
    context_id: str | None = None


class DirectivesResult(Wire):
    directives: list[DirectiveLayer]


class SetDirectivesInput(Wire):
    scope: Scope
    ref: str | None = None
    items: list[DirectiveItem]


class SendInput(Wire):
    to: str
    body: Body
    context_id: str | None = None


class SendResult(Wire):
    message_id: str
    context_id: str
    status: SendStatus
    error: str | None = None


class RespondInput(Wire):
    context_id: str
    body: Body


class InboxInput(Wire):
    since: str | None = None
    contact: str | None = None
    intent: str | None = None
    include_review: bool = False
    limit: int = 50


class InboxItem(Wire):
    message_id: str
    context_id: str
    from_: str = Field(alias="from")
    received_at: str
    status: InboundStatus
    body: Body


class InboxResult(Wire):
    items: list[InboxItem]
    next_since: str | None


class Event(Wire):
    event_id: str
    event: str
    occurred_at: str
    data: dict[str, Any]


class InboxWaitInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    timeout_seconds: int = 30
    last_event_id: str | None = None


class InboxWaitResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    events: list[Event]
    next_event_id: str | None


class ContextsInput(Wire):
    contact: str | None = None
    include_review: bool = False
    since: str | None = None
    limit: int = 50


class Context(Wire):
    context_id: str
    peers: list[str]
    last_message_at: str
    last_direction: Direction
    last_intent: str | None = None


class ContextsResult(Wire):
    contexts: list[Context]
    next_since: str | None


class HistoryInput(Wire):
    context_id: str | None = None
    contact: str | None = None
    intent: str | None = None
    include_review: bool = False
    before: str | None = None
    limit: int = 50


class HistoryItem(Wire):
    message_id: str
    context_id: str
    direction: Direction
    peer: str
    occurred_at: str
    status: InboundStatus | OutboundStatus
    body: Body


class HistoryResult(Wire):
    items: list[HistoryItem]
    next_before: str | None


class InboxMessageEventData(Wire):
    message_id: str
    context_id: str
    from_: str = Field(alias="from")
    intent: str | None = None
    status: InboundStatus


class OutboxStatusEventData(Wire):
    message_id: str
    context_id: str
    to: str
    status: Literal["accepted", "failed"]


class DirectivesUpdatedEventData(Wire):
    scope: Scope
    ref: str | None = None


class TaskUpdateEventData(Wire):
    context_id: str
    task_id: str
    status: str


NOTIFICATION_NAMESPACE = "notifications/shadownet/"

EVENT_DATA_MODELS: dict[str, type[Wire]] = {
    "inbox.message": InboxMessageEventData,
    "outbox.status": OutboxStatusEventData,
    "directives.updated": DirectivesUpdatedEventData,
    "task.update": TaskUpdateEventData,
}

TOOLS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "identity": (Wire, IdentityResult),
    "resolve": (ResolveInput, ResolveResult),
    "contacts": (ContactsInput, ContactsResult),
    "contact_detail": (ContactDetailInput, ContactDetail),
    "add_contact": (AddContactInput, AddContactResult),
    "grant": (GrantInput, Ok),
    "set_contact_profile": (SetContactProfileInput, Ok),
    "directives": (DirectivesInput, DirectivesResult),
    "set_directives": (SetDirectivesInput, Ok),
    "send": (SendInput, SendResult),
    "respond": (RespondInput, SendResult),
    "inbox": (InboxInput, InboxResult),
    "inbox_wait": (InboxWaitInput, InboxWaitResult),
    "contexts": (ContextsInput, ContextsResult),
    "history": (HistoryInput, HistoryResult),
}

TOOL_ERRORS: dict[str, tuple[str, ...]] = {
    "resolve": ("resolve_failed", "unreachable"),
    "contact_detail": ("not_contact",),
    "add_contact": ("resolve_failed", "already_contact"),
    "grant": ("not_contact", "unknown_grant"),
    "set_contact_profile": ("not_contact",),
    "set_directives": ("not_contact", "unknown_context"),
}
