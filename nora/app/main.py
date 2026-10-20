"""FastAPI application entrypoint.

The app factory enforces the boot gate (operator acknowledgment + required
credentials) before wiring routes. The inbound webhook verifies and authenticates
synchronously, returns a fast ack to the provider, and runs the drafting pipeline
in the background (drafting via Claude takes seconds; providers expect a quick
response). Dependencies are injectable so the pipeline is testable without
network access.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import BackgroundTasks, FastAPI, Request, Response

from app import __version__
from app.alerts import AlertThrottle, maybe_send_failure_alert
from app.calendar import CalendarClient, NullCalendarClient
from app.filesource import FileSource, NullFileSource
from app.config import Settings, get_settings
from app.research import NullResearchClient
from app.tenant import NullTenantDraftClient
from app.drafting.claude import DraftingService
from app.email.inbound import InboundOutcome, process_inbound
from app.email.outbound import OutboundSender
from app.email.postmark_inbound import adapt_postmark_inbound
from app.email.postmark_send import PostmarkSendClient
from app.logging import configure_logging, get_logger, redact
from app.metrics import emit
from app.models import InboundEmail, NoraResult, ResultKind
from app.pipeline import ActionDescriptor, Pipeline
from app.receipt import ComposedReply, compose_reply
from app.security import RateLimiter, ReplayGuard, resolve_client_ip
from app.state.outbox import FileSendOutbox, InMemorySendOutbox, OutboxRecord, SendOutbox
from app.state.store import FilePendingActionStore, FileTriageStore, InMemoryStateStore

logger = get_logger(__name__)


@dataclass
class Dependencies:
    pipeline: Pipeline
    outbound: OutboundSender
    # Throttles the real-time ops failure alert (single-instance, in-memory).
    alert_throttle: AlertThrottle = field(default_factory=lambda: AlertThrottle(900))
    outbox: SendOutbox = field(default_factory=InMemorySendOutbox)
    calendar: CalendarClient = field(default_factory=NullCalendarClient)
    # File source for re-fetching a consent-gated shared file at send time (never persisted).
    file_source: FileSource = field(default_factory=NullFileSource)


def build_default_dependencies(settings: Settings) -> Dependencies:
    """Production wiring: real Claude client, in-memory store, provider sender."""
    if settings.drafting_backend == "ollama":
        from app.drafting.ollama import OllamaDraftingService
        drafting = OllamaDraftingService(
            settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout
        )
    elif settings.drafting_backend == "openrouter":
        from app.drafting.openrouter import OpenRouterDraftingService
        drafting = OpenRouterDraftingService(
            settings.openrouter_base_url, settings.openrouter_api_key,
            settings.openrouter_model, settings.openrouter_timeout,
        )
    else:
        import anthropic

        # Bounded timeout + retries so a slow/hung Claude call can't pin a background
        # worker indefinitely (reliability).
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key, timeout=30.0, max_retries=2
        )
        drafting = DraftingService(client, settings.claude_model)
    # Local in-boundary backend for R3 relationships (default None => R3 declines). Separate
    # from `drafting` (R1/Claude) so both can run in one prod process.
    local_drafting = None
    if settings.local_drafting_backend == "ollama":
        from app.drafting.ollama import OllamaDraftingService
        local_drafting = OllamaDraftingService(
            settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout
        )
    store = InMemoryStateStore(settings.state_ttl_seconds)
    file_source = _build_file_source(settings)
    calendar = _build_calendar_client(settings)  # built once; shared for propose-time
    # conflict reads (pipeline) and confirm-time booking (Dependencies).
    pipeline = Pipeline(
        settings=settings, drafting=drafting, store=store,
        actions=FilePendingActionStore(settings.pending_action_path, settings.pending_action_ttl_seconds),
        research=_build_research_client(settings),
        tenant=_build_tenant_client(settings),
        local_drafting=local_drafting,
        memory=_build_memory_store(settings),
        voice=_build_voice_store(settings),
        users=_build_user_store(settings),
        triage_sessions=FileTriageStore(settings.triage_session_path, settings.state_ttl_seconds),
        interpreter=_build_interpreter(settings),
        file_source=file_source,
        local_file_source=_build_local_file_source(settings),
        calendar=calendar,
    )
    outbound = OutboundSender(_build_send_client(settings))
    return Dependencies(
        pipeline=pipeline,
        outbound=outbound,
        file_source=file_source,
        alert_throttle=AlertThrottle(settings.alert_throttle_seconds),
        outbox=FileSendOutbox(settings.outbox_path),
        calendar=calendar,
    )


def _build_file_source(settings: Settings):  # noqa: ANN202 — returns FileSource
    """Select the files-domain access backend. Default "none" → NullFileSource (off)."""
    if (
        settings.file_source_backend == "gdrive"
        and settings.gmail_credentials_json
        and settings.gmail_tenant_user
    ):
        from app.filesource import (
            GoogleDriveFileSource,
            build_drive_service,
            build_drive_write_service,
        )

        service = build_drive_service(settings.gmail_credentials_json, settings.gmail_tenant_user)
        # Default-off write seam: only when explicitly enabled do we hold a full-`drive`
        # service for consent-gated writes (PR.SV save, SH.G grant). Reads stay read-only.
        write_service = (
            build_drive_write_service(settings.gmail_credentials_json, settings.gmail_tenant_user)
            if settings.file_source_writable
            else None
        )
        return GoogleDriveFileSource(service=service, write_service=write_service)
    if settings.file_source_backend == "graph" and settings.graph_tenant_user:
        from app.filesource import GraphFileSource
        from app.tenant import build_graph_session

        session = build_graph_session(
            settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret
        )
        return GraphFileSource(settings.graph_tenant_user, session=session)
    from app.filesource import NullFileSource

    return NullFileSource()


def _build_local_file_source(settings: Settings):  # noqa: ANN202 — returns FileSource
    """R3 in-boundary file source. NORA_FILEVAULT_ROOT set → LocalFileVault (path-contained,
    read-only, never egresses); empty → NullFileSource (off)."""
    if settings.filevault_root:
        from app.filesource import LocalFileVault

        return LocalFileVault(settings.filevault_root)
    return NullFileSource()


def _build_memory_store(settings: Settings):  # noqa: ANN202 — returns MemoryStore
    """Select cross-thread memory. Default "none" → NullMemoryStore (off)."""
    if settings.memory_backend == "file":
        from app.memory import FileMemoryStore

        return FileMemoryStore(settings.memory_path)
    from app.memory import NullMemoryStore

    return NullMemoryStore()


def _build_interpreter(settings: Settings):  # noqa: ANN202 — returns Interpreter
    """Plain-language interpreter. Default 'keyword' -> NullInterpreter (off; route()'s keyword
    fallback handles NL). 'llm' -> the R1/cloud LLMInterpreter (KL/R3 still use the keyword path)."""
    if settings.nl_interpreter_backend == "llm" and settings.anthropic_api_key:
        import anthropic

        from app.nl_interpreter import LLMInterpreter

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return LLMInterpreter(client, model=settings.nl_interpreter_model)
    if settings.nl_interpreter_backend == "openrouter" and settings.openrouter_api_key:
        from app.nl_interpreter import OpenRouterInterpreter

        return OpenRouterInterpreter(
            settings.openrouter_base_url, settings.openrouter_api_key, settings.openrouter_model
        )
    from app.nl_interpreter import NullInterpreter

    return NullInterpreter()


def _build_user_store(settings: Settings):  # noqa: ANN202 — returns UserStore
    """Dynamic user registry for R1 self-serve onboarding. Default off (NullUserStore) until an
    invite allowlist is configured; then file-backed so provisioned users survive a restart."""
    if settings.signup_allowlist:
        from app.users import FileUserStore

        return FileUserStore(settings.users_path)
    from app.users import NullUserStore

    return NullUserStore()


def _build_voice_store(settings: Settings):  # noqa: ANN202 — returns VoiceStore
    """Select Layer B voice memory. Default off → NullVoiceStore (records/recalls nothing)."""
    if settings.voice_memory:
        from app.voice_memory import FileVoiceStore

        return FileVoiceStore(settings.voice_path, max_signals=settings.voice_max_signals)
    from app.voice_memory import NullVoiceStore

    return NullVoiceStore()


def _build_tenant_client(settings: Settings):  # noqa: ANN202 — returns TenantDraftClient
    """Select the R2 in-tenant draft backend. Default "none" → declines (no R1 processing).
    "gmail" saves drafts in the client's own Gmail Drafts (mail stays in their tenant)."""
    if (
        settings.tenant_backend == "gmail"
        and settings.gmail_credentials_json
        and settings.gmail_tenant_user
    ):
        from app.tenant import GmailDraftTenantClient, build_gmail_service

        service = build_gmail_service(settings.gmail_credentials_json, settings.gmail_tenant_user)
        return GmailDraftTenantClient(settings.gmail_tenant_user, service=service)
    if settings.tenant_backend == "graph" and settings.graph_tenant_user:
        from app.tenant import GraphDraftTenantClient, build_graph_session

        session = build_graph_session(
            settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret
        )
        return GraphDraftTenantClient(settings.graph_tenant_user, session=session)
    return NullTenantDraftClient()


def _build_research_client(settings: Settings):  # noqa: ANN202 — returns ResearchClient
    """Select the web-research backend. Default "none" → NullResearchClient (declines).
    "claude" uses Claude's hosted web search; "deep" runs the multi-angle, fact-checked
    DeepResearchClient (a few more calls, a sourced briefing). Both egress to Claude + the
    web and are default-off."""
    if settings.research_backend == "claude" and settings.anthropic_api_key:
        from app.research import ClaudeWebResearchClient

        return ClaudeWebResearchClient(settings.anthropic_api_key, settings.claude_model)
    if settings.research_backend == "deep" and settings.anthropic_api_key:
        from app.research import DeepResearchClient

        return DeepResearchClient(settings.anthropic_api_key, settings.claude_model)
    return NullResearchClient()


def _build_calendar_client(settings: Settings):  # noqa: ANN202 — returns CalendarClient
    """Select the calendar backend. Default "none" → NullCalendarClient (declines)."""
    if settings.calendar_backend == "google" and settings.google_credentials_json:
        from app.calendar import GoogleCalendarClient, build_google_calendar_service

        # Impersonate the tenant mailbox (DWD) so `primary` is her calendar and the invite
        # is organized from her own labeled identity — same subject as the Gmail/Drive seams.
        service = build_google_calendar_service(
            settings.google_credentials_json, subject=settings.gmail_tenant_user or None
        )
        return GoogleCalendarClient(settings.google_calendar_id, service=service)
    return NullCalendarClient()


def _build_send_client(settings: Settings):  # noqa: ANN202 — returns EmailProviderClient
    """Pick the outbound transport: Resend/Postmark (domain DKIM), SMTP (dev), or Gmail
    (showcase — sends as the Workspace mailbox)."""
    if settings.transport == "resend":
        from app.email.resend_send import ResendSendClient

        return ResendSendClient(settings.resend_api_key, settings.postmark_sender)
    if settings.transport == "gmail":
        from app.email.gmail_send import GmailSendClient

        # OAuth (personal Gmail, e.g. ambient auto-draft) has no service-account key — build the
        # service from the OAuth token instead. That token is gmail.modify only (no send scope),
        # so this client structurally cannot send; auto-draft never calls it, it only drafts.
        if settings.gmail_auth == "oauth":
            from app.email.gmail_oauth import build_gmail_oauth_service

            service = build_gmail_oauth_service(settings.gmail_oauth_token)
        else:
            from app.email.gmail_inbound import build_gmail_read_service

            service = build_gmail_read_service(
                settings.gmail_credentials_json, settings.gmail_tenant_user
            )
        return GmailSendClient(service=service, sender=settings.gmail_tenant_user)
    if settings.transport == "smtp":
        from app.email.smtp_send import SmtpSendClient

        return SmtpSendClient(
            settings.smtp_host,
            settings.smtp_port,
            settings.postmark_sender,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
        )
    return PostmarkSendClient(settings.provider_api_key, settings.postmark_sender)


def _sender_for(settings: Settings, email: InboundEmail) -> str | None:
    """The From address Nora replies with. For domain-DKIM transports (resend /
    postmark) it's the person's own domain address — <local-part>@<domain> — so the
    reply comes from e.g. democeo@nora-bennett.com. The domain is address_domain
    unless this mailbox has a sender_domain_overrides entry (reply-as-client-domain,
    e.g. nora.bennett -> example.nl), which requires the provider to be verified for
    that domain. For the smtp/dev transport, None lets the client use its configured
    (Gmail) From, since Gmail can't legitimately sign as the brand domain."""
    if settings.transport in ("resend", "postmark"):
        domain = settings.sender_domain_overrides.get(
            email.secret_address_local, settings.address_domain
        )
        return f"{email.secret_address_local}@{domain}"
    return None


def _ceo_address(settings: Settings, email: InboundEmail, user_store: object = None) -> str:
    """Resolve the send destination from the authoritative registry (KTD6), so a
    spoofed From can never steer where Nora replies.

    ``_run_pipeline`` only runs on authenticated emails, so ``authenticate()`` has
    already proven the local part is a registry key and the From is one of its
    allowed addresses. For a single registered address we return it directly; for a
    person with several allowed addresses we reply to the one they forwarded from
    (already verified to be in the allow-list — not attacker-controlled)."""
    if settings.signup_local and email.secret_address_local == settings.signup_local:
        return email.from_address  # signup reply goes to the (DKIM-verified) signer
    from app.users import effective_registry

    registered = effective_registry(settings.ceo_registry, user_store)[email.secret_address_local]
    if isinstance(registered, str):
        return registered
    if isinstance(registered, list) and len(registered) == 1:
        return registered[0]
    return email.from_address


def _run_pipeline(deps: Dependencies, settings: Settings, email: InboundEmail) -> None:
    """Background task: draft and send the reply to the CEO.

    The provider already received a 202, so it will not retry. On any failure we
    send a best-effort failure receipt to the CEO rather than leaving them with
    silence (the worst outcome for a trust product).
    """
    ceo_address = _ceo_address(settings, email, getattr(deps.pipeline, "users", None))
    sender = _sender_for(settings, email)
    started = time.monotonic()
    try:
        reply, reply_to, action = deps.pipeline.handle(email)
        if reply is not None:
            deps.outbound.send_to_ceo(ceo_address, reply, reply_to=reply_to, sender=sender)
        if action is not None:
            # The one consent-gated third-party send (CEO already confirmed AG). Runs
            # the send + composes the CEO outcome receipt from the actual result.
            _execute_action(deps, settings, email, ceo_address, sender, action)
        emit(
            "send",
            ceo_id=email.secret_address_local,
            salt=settings.metrics_salt,
            outcome="sent",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:  # noqa: BLE001 — never let a background failure crash silently
        logger.exception("pipeline failed for event %s", redact({"event_id": email.event_id}))
        emit(
            "send",
            ceo_id=email.secret_address_local,
            salt=settings.metrics_salt,
            outcome="failure",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            failure = ComposedReply(
                subject="Nora · couldn't process that",
                body="Something went wrong on my end preparing that — nothing was sent. "
                "Please forward the thread again to retry.",
            )
            deps.outbound.send_to_ceo(ceo_address, failure, reply_to=None, sender=sender)
        except Exception:  # noqa: BLE001 — failure receipt is best-effort
            logger.exception("failure receipt also failed for %s", redact({"event_id": email.event_id}))
        # Real-time ops alert to the maintainer (best-effort, throttled, redacted).
        maybe_send_failure_alert(
            deps.outbound, deps.alert_throttle, settings,
            email.secret_address_local, email.event_id, ceo_address, sender,
        )


def _perform_send(deps: Dependencies, settings: Settings, entry: OutboxRecord) -> None:
    """Send one consent-gated third-party email from a durable outbox record, then
    receipt the CEO and clear the record. Shared by the live confirm path and boot
    replay. From is Nora's labeled address (assistant_address, fallback brand From) —
    never the CEO's per-person address (R8). The success receipt is sent only AFTER the
    provider returns (KTD-D); a caught failure yields one accurate "couldn't send"
    receipt + ops alert. The record is removed on BOTH outcomes — it survives only a
    crash, which boot replay then completes."""
    from_address = settings.assistant_address or settings.postmark_sender
    proposal = ComposedReply(subject=entry.subject, body=entry.body)
    try:
        # Consent-gated file share, performed NOW (after AG):
        #  - "grant": grant the recipient read access in the store (silent — no store email)
        #    and send the link from Nora's own address.
        #  - "attach": re-fetch the file (never persisted) and attach it.
        # Either failure (file gone / grant failed) raises → the failure path below receipts
        # the CEO rather than sending an empty/wrong share. Fail closed.
        attachments = None
        if entry.file_ref and entry.share_mode == "grant":
            link = deps.file_source.grant(entry.file_ref, entry.recipient)
            if not link:
                raise RuntimeError(f"could not grant access to: {entry.file_ref}")
            proposal = ComposedReply(subject=entry.subject, body=f"{entry.body}\n\nAccess link: {link}")
        elif entry.file_ref:
            fetched = deps.file_source.fetch(entry.file_ref)
            if fetched is None:
                raise RuntimeError(f"shared file not found: {entry.file_ref}")
            attachments = [fetched]
        deps.outbound.send_on_behalf(
            entry.recipient, proposal,
            sender=from_address, reply_to=entry.ceo_address, idempotency_key=entry.token,
            attachments=attachments,
        )
    except Exception:  # noqa: BLE001 — a dropped confirmed send must never be silent
        logger.exception(
            "confirmed third-party send failed for %s",
            redact({"event_id": entry.event_id, "recipient": entry.recipient}),
        )
        # Remove the record first so a crash in the receipt/alert path below never
        # causes a boot-replay to re-attempt a failed (not merely stranded) send.
        deps.outbox.remove(entry.token)
        failure = ComposedReply(
            subject=f"Nora · couldn't send to {entry.recipient}",
            body=(
                f"I couldn't send your meeting proposal to {entry.recipient} just now — "
                "nothing went out. Forward the thread again and reply AG to retry."
            ),
        )
        try:
            deps.outbound.send_to_ceo(entry.ceo_address, failure, reply_to=None, sender=entry.ceo_sender)
        except Exception:  # noqa: BLE001 — failure receipt is best-effort
            logger.exception("action failure receipt also failed for %s", redact({"event_id": entry.event_id}))
        maybe_send_failure_alert(
            deps.outbound, deps.alert_throttle, settings,
            entry.ceo_id, entry.event_id, entry.ceo_address, entry.ceo_sender,
        )
        emit("action", ceo_id=entry.ceo_id, salt=settings.metrics_salt, phase="failed")
        return
    receipt = compose_reply(
        NoraResult(
            kind=ResultKind.ACTION_SENT,
            text=(
                f"Done — I sent your meeting proposal to {entry.recipient} from my own "
                "assistant address. Their reply will come straight to you."
            ),
            recipient=entry.recipient,
        )
    )
    deps.outbound.send_to_ceo(entry.ceo_address, receipt, reply_to=None, sender=entry.ceo_sender)
    emit("action", ceo_id=entry.ceo_id, salt=settings.metrics_salt, phase="sent")
    deps.outbox.remove(entry.token)


def _execute_calendar_hold(
    deps: Dependencies, settings: Settings, email: InboundEmail,
    ceo_address: str, sender: str | None, action: ActionDescriptor,
) -> None:
    """Execute a confirmed calendar hold. Calls the CalendarClient (may be Null);
    receipts the CEO with an outcome either way. Never goes through the email outbox."""
    from app.calendar import CalendarEvent, CalendarUnavailable

    payload = action.payload
    event = CalendarEvent(
        summary=payload.get("summary", action.subject),
        start=payload.get("start", ""),
        end=payload.get("end", ""),
        attendees=payload.get("attendees", [action.recipient]),
    )
    try:
        deps.calendar.create_event(event, invite=bool(payload.get("invite")))
    except CalendarUnavailable:
        notice = ComposedReply(
            subject="Nora · calendar not available",
            body=(
                "Calendar holds aren't available in this version — nothing was scheduled. "
                "You can create the event manually on your calendar. Forward the thread "
                "again and reply AG once calendar integration is set up."
            ),
        )
        emit(
            "action",
            ceo_id=email.secret_address_local,
            salt=settings.metrics_salt,
            phase="calendar_unavailable",
        )
        deps.outbound.send_to_ceo(ceo_address, notice, reply_to=None, sender=sender)
        return

    attendees_str = ", ".join(event.attendees) if event.attendees else action.recipient
    receipt = compose_reply(
        NoraResult(
            kind=ResultKind.ACTION_SENT,
            text=(
                f"Held: {event.summary} with {attendees_str}. "
                "(Confirm the exact time on your calendar.)"
            ),
            recipient=action.recipient,
        )
    )
    emit(
        "action",
        ceo_id=email.secret_address_local,
        salt=settings.metrics_salt,
        phase="calendar_held",
    )
    deps.outbound.send_to_ceo(ceo_address, receipt, reply_to=None, sender=sender)


def _execute_action(
    deps: Dependencies, settings: Settings, email: InboundEmail,
    ceo_address: str, sender: str | None, action: ActionDescriptor,
) -> None:
    """Dispatch a confirmed action by kind. Email sends go through the durable outbox
    (write-before-send); calendar holds go directly to the CalendarClient seam.
    The email path is byte-unchanged for action_kind='send_email'."""
    if action.action_kind == "calendar_hold":
        _execute_calendar_hold(deps, settings, email, ceo_address, sender, action)
        return
    # send_email (default) or share_file — both go through the durable outbox + the one
    # consent-gated send_on_behalf path. share_file carries a file_ref re-fetched at send.
    entry = OutboxRecord(
        token=action.token, recipient=action.recipient, subject=action.subject,
        body=action.body, ceo_address=ceo_address, ceo_sender=sender,
        ceo_id=email.secret_address_local, event_id=email.event_id,
        file_ref=action.payload.get("file_ref") if action.action_kind == "share_file" else None,
        share_mode=action.payload.get("share_mode", "attach"),
    )
    try:
        deps.outbox.record(entry)
    except Exception:  # noqa: BLE001 — durability is best-effort; never block the send
        logger.exception("outbox write failed for %s", redact({"event_id": email.event_id}))
    # Write-before-consume: mark the proposal single-use ONLY now that its durable outbox record
    # exists. A crash before this leaves the proposal pending → the send is recovered (outbox
    # replay / a re-AG), never silently lost; a duplicate is prevented by the idempotency-keyed send.
    _consume_pending_action(deps, action.token)
    _perform_send(deps, settings, entry)


def _consume_pending_action(deps: Dependencies, token: str) -> None:
    """Mark a pending action consumed (single-use). No-op if already consumed/absent."""
    rec = deps.pipeline.actions.get(token)
    if rec is not None and rec.status == "pending":
        rec.status = "consumed"
        deps.pipeline.actions.put(rec)


def _replay_outbox(deps: Dependencies, settings: Settings) -> None:
    """At boot, complete any send stranded by a crash in the consume->send window.
    Per-record best-effort; _perform_send handles its own send failures, and the
    guard here ensures one bad record never blocks startup."""
    stranded = deps.outbox.pending()
    if not stranded:
        return
    # A recovered confirmed send is exactly the event an operator wants to see —
    # never silent (count only; the recipient/content stay out of the log).
    logger.info("outbox replay: %d stranded confirmed send(s) at boot", len(stranded))
    # Safe replay requires an idempotent transport: the provider dedupes so a record
    # stranded AFTER the original send already went out won't double-send. On a
    # non-idempotent transport (Postmark/SMTP) we must NOT resend — a duplicate to a
    # real third party is worse than a possible miss — so we discard with a CEO notice.
    idempotent = deps.outbound.supports_idempotency
    for entry in stranded:
        try:
            if idempotent:
                _perform_send(deps, settings, entry)
            else:
                _discard_stranded_send(deps, settings, entry)
        except Exception:  # noqa: BLE001 — replay must never crash boot
            logger.exception("outbox replay failed for %s", redact({"event_id": entry.event_id}))


def _discard_stranded_send(deps: Dependencies, settings: Settings, entry: OutboxRecord) -> None:
    """Non-idempotent transport: do NOT resend a stranded consent-gated proposal (a
    duplicate to a real third party is the worse failure). Remove the record so it
    can't replay, tell the CEO it may not have completed, and alert ops."""
    deps.outbox.remove(entry.token)
    notice = ComposedReply(
        subject=f"Nora · please check your proposal to {entry.recipient}",
        body=(
            f"I restarted while sending your meeting proposal to {entry.recipient}, and to "
            "avoid sending it twice I did NOT resend it. It may or may not have gone out — "
            f"please check with {entry.recipient}, or forward the thread and reply AG to send "
            "again."
        ),
    )
    try:
        deps.outbound.send_to_ceo(entry.ceo_address, notice, reply_to=None, sender=entry.ceo_sender)
    except Exception:  # noqa: BLE001 — the CEO notice is best-effort
        logger.exception("stranded-send notice failed for %s", redact({"event_id": entry.event_id}))
    maybe_send_failure_alert(
        deps.outbound, deps.alert_throttle, settings,
        entry.ceo_id, entry.event_id, entry.ceo_address, entry.ceo_sender,
    )
    emit("action", ceo_id=entry.ceo_id, salt=settings.metrics_salt, phase="stranded_not_resent")


def create_app(settings: Settings | None = None, deps: Dependencies | None = None) -> FastAPI:
    """Build the FastAPI app. Raises at construction if not safe to boot."""
    settings = settings or get_settings()
    settings.require_bootable()
    configure_logging()  # ensure app.* INFO logs are emitted (survives uvicorn)
    deps = deps or build_default_dependencies(settings)
    _replay_outbox(deps, settings)  # complete any crash-stranded confirmed send

    app = FastAPI(title="Nora", version=__version__)
    app.state.settings = settings
    app.state.deps = deps
    # Align replay protection with the action/revise TTL: a body replayed up to
    # state_ttl_seconds later could otherwise re-run the pipeline (Claude cost, a
    # re-emitted reply, a fresh revise token) while the original state is still
    # actionable. Past the TTL the state has expired anyway.
    app.state.replay_guard = ReplayGuard(window_seconds=settings.state_ttl_seconds)
    app.state.rate_limiter = RateLimiter()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/inbound/webhook")
    async def inbound_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
        raw_body = await request.body()
        # Defense-in-depth: reject oversized bodies before any parsing/auth so
        # untrusted input can't drive unbounded work (size only — never content).
        max_bytes = app.state.settings.max_inbound_bytes
        if max_bytes and len(raw_body) > max_bytes:
            logger.warning("inbound rejected: body too large (%d bytes)", len(raw_body))
            return Response(status_code=413)
        # Behind the cloudflared tunnel the socket peer is always 127.0.0.1; the
        # real client IP rides in CF-Connecting-IP. Without this, all inbound mail
        # shares one IP rate-limit bucket and one burst can 429 every CEO.
        client_key = resolve_client_ip(
            request.headers.get("cf-connecting-ip"),
            request.client.host if request.client else None,
        )
        outcome: InboundOutcome = process_inbound(
            raw_body=raw_body,
            authorization_header=request.headers.get("authorization"),
            client_key=client_key,
            settings=app.state.settings,
            replay_guard=app.state.replay_guard,
            rate_limiter=app.state.rate_limiter,
            adapt=adapt_postmark_inbound,
            user_store=getattr(app.state.deps.pipeline, "users", None),
        )
        if outcome.email is not None:
            logger.info("inbound accepted: %s", redact({"event_id": outcome.email.event_id}))
            background_tasks.add_task(_run_pipeline, app.state.deps, app.state.settings, outcome.email)
        return Response(status_code=outcome.status_code)

    return app
