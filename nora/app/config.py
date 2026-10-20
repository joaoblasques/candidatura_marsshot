"""Application configuration.

Settings load from environment variables (prefixed ``NORA_`` where applicable).
In production, secret values come from a secrets manager rather than a committed
file (see KTD10 in the plan). The app refuses to start unless the operator has
acknowledged the v1.0 external-LLM stance (KTD5).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NORA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Operator acknowledgment gate (KTD5). The service must not boot without it.
    external_llm_ack: bool = Field(
        default=False,
        description="Operator acknowledges that forwarded thread content is processed "
        "by an external LLM (Claude) and briefly stored in v1.0.",
    )

    # Anthropic (not NORA_-prefixed — matches the SDK's conventional env var).
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    claude_model: str = "claude-sonnet-4-6"

    # Outbound transport: "resend" or "postmark" (both send from the brand domain
    # with domain DKIM — From is the person's <local-part>@address_domain), or
    # "smtp" (local demo / Gmail; static From). The inbound webhook is identical.
    transport: str = "postmark"

    # Resend (transport="resend"): API key; From comes from the per-person domain
    # address, so no static sender is needed for this transport.
    resend_api_key: str = ""

    # Postmark (inbound parse + outbound send).
    # provider_api_key is the Postmark server token (X-Postmark-Server-Token),
    # used for outbound send. webhook_basic_auth is the expected HTTP Basic Auth
    # credential ("user:password") for the inbound webhook — Postmark POSTs to a
    # Basic-Auth URL, replacing the v1.0 HMAC body-signature (KTD4). postmark_sender
    # is the outbound From address (a Postmark verified sender signature in prod,
    # or any From for the smtp transport).
    provider_api_key: str = ""
    webhook_basic_auth: str = ""
    postmark_sender: str = ""

    # SMTP transport (when transport="smtp"). host/port point at Mailpit locally
    # or smtp.gmail.com:587 for a real send; username/password + use_tls for auth.
    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False

    # Domain Nora receives at; used to build the plus-addressed Reply-To (KTD2/KTD6).
    # For the Postmark pilot this is the inbound hash-inbox domain, so the Reply-To
    # is <server-hash>+<token>@inbound.postmarkapp.com.
    address_domain: str = "inbound.postmarkapp.com"

    # Nora's own labeled From for consent-gated third-party sends (0.2.0), e.g.
    # nora@nora-bennett.com — never the CEO's address. Empty falls back to
    # postmark_sender (the required brand From), so the action send From is never empty.
    assistant_address: str = ""

    # Layer B per-person voice memory (default off). Stores only coded refinement signals.
    voice_memory: bool = False
    voice_path: str = ".nora-voice.json"
    voice_signal_ttl_seconds: int = 7_776_000  # 90-day decay window
    voice_max_signals: int = 200

    # Revise-loop state.
    state_ttl_seconds: int = 86_400
    max_revise_rounds: int = 5
    # Pending consent-gated proposals live longer than revise records so a late AG still finds
    # them and the follow-up loop can remind before they lapse.
    pending_action_ttl_seconds: int = 432_000  # 5 days
    followup_remind_after_seconds: int = 172_800  # 2 days
    followup_max_reminders: int = 1

    # Durable send outbox: file path for the one in-flight consent-gated send, so a
    # crash between consume-and-send is recoverable at boot (KTD-D). Default is a
    # gitignored local file; not boot-required.
    outbox_path: str = ".nora-outbox.json"

    # Durable pending-action store: file path for pre-confirm proposals so they survive
    # a restart between propose and AG. Default is a gitignored local file; not boot-required.
    pending_action_path: str = ".nora-pending-actions.json"

    # Durable triage-session store (TR v2): the split items behind a digest, so a reply like
    # "2 RE" survives a restart between the digest and the follow-up. Gitignored; not boot-required.
    triage_session_path: str = ".nora-triage.json"

    # Reject inbound webhook bodies larger than this many bytes before any parsing
    # (defense-in-depth: bounds the work untrusted input can trigger). 0 disables.
    # Sized to fit base64-encoded attachments (~13.3MB of files) plus the body.
    max_inbound_bytes: int = 15_000_000

    # Salt for the non-reversible metric identity hash (ceo_hash / thread_key).
    # Keeps per-CEO grouping in metric events without storing identity. Set in the
    # environment (NORA_METRICS_SALT); empty = a safe default (still hashed).
    metrics_salt: str = ""

    # Real-time ops failure alert: when the pipeline fails, email this maintainer
    # address (empty disables it). Throttled to avoid alert storms when a
    # dependency (Claude/Resend) is down.
    alert_email: str = ""
    alert_throttle_seconds: int = 900

    # Calendar hold action backend: "none" (default, disabled) or "google". A confirmed
    # calendar hold declines gracefully when "none" or when the time is unresolved.
    calendar_backend: str = "none"
    # Google Calendar backend (when calendar_backend="google"): the target calendar id
    # and a service-account JSON key file path. Both required to activate.
    google_calendar_id: str = "primary"
    google_credentials_json: str = ""
    # Timezone for resolving natural-language calendar times (e.g. "Tue 3pm"), so a hold's
    # instant is deterministic and host-independent. An IANA name, e.g. "Europe/Amsterdam".
    timezone_name: str = "UTC"

    # Web-research backend (FI/I): "none" (default, disabled) or "claude" (Claude's
    # hosted web search — egresses counterparty info to Claude + the web). FI/I decline
    # gracefully (NullResearchClient) when "none".
    research_backend: str = "none"

    # Plain-language interpreter: "keyword" (deterministic fallback, default) or "llm" (R1 cloud —
    # turns free-form English into the same routed request a code produces). KL/R3 always use the
    # keyword path (no cloud). Flip to "llm" to make plain language primary.
    nl_interpreter_backend: str = "keyword"
    nl_interpreter_model: str = "claude-haiku-4-5-20251001"

    # In-tenant draft backend (R2): "none" (default, disabled), "gmail" (Google Workspace
    # Drafts via a domain-wide-delegated service account), or "graph" (Microsoft 365 Outlook
    # Drafts via Microsoft Graph app-only auth). Declines gracefully when "none".
    tenant_backend: str = "none"
    # Gmail tenant backend (when tenant_backend="gmail"): service-account key path + the
    # tenant mailbox to impersonate (drafts land in that mailbox's Drafts).
    gmail_credentials_json: str = ""
    gmail_tenant_user: str = ""
    # Gmail auth mode for the poller/auto-draft: "service_account" (Workspace + domain-wide
    # delegation, default) or "oauth" (a personal/consumer @gmail.com via an installed-app OAuth
    # consent — a service account can't impersonate a consumer account). For "oauth": a client
    # secret JSON (from a Google Cloud OAuth client) + a token file written by the one-time
    # `scripts.gmail_oauth_login`. Env: NORA_GMAIL_AUTH / NORA_GMAIL_OAUTH_CLIENT / NORA_GMAIL_OAUTH_TOKEN.
    gmail_auth: str = "service_account"
    gmail_oauth_client: str = ""
    gmail_oauth_token: str = ".nora-gmail-token.json"
    # Microsoft Graph tenant backend (when tenant_backend="graph"): Azure AD app
    # (client-credentials) + the tenant mailbox whose Drafts receive the reply.
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_tenant_user: str = ""
    # Showcase Gmail-pull inbound: the trusted authserv-id Gmail stamps on received mail,
    # used to derive dkim_verified (see app/email/gmail_inbound.py).
    gmail_trusted_authserv_id: str = "mx.google.com"

    # Drafting backend: "anthropic" (default, prod), "ollama" (local, DEV-ONLY), or
    # "openrouter" (OpenAI-compatible cloud, dev/demo — free/cheap models when no Anthropic key).
    drafting_backend: str = "anthropic"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout: float = 120.0
    # OpenRouter (used by drafting_backend/nl_interpreter_backend="openrouter"). One key +
    # model power both drafting and the plain-language interpreter.
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_timeout: float = 60.0

    # Local (in-boundary) drafting backend for R3 relationships: "none" (default, disabled —
    # R3 declines) or "ollama" (R3 producing intents draft on local hardware via the ollama_*
    # settings, never the external Claude API). SEPARATE from `drafting_backend`: R1 keeps using
    # Claude, so this does NOT trigger the dev-only boot guard above.
    local_drafting_backend: str = "none"

    # CEO registry: secret-address local-part -> registered From address(es).
    # A value may be a single address or a list of allowed addresses for one
    # person (e.g. someone who forwards from several accounts).
    ceo_registry: dict[str, str | list[str]] = Field(default_factory=dict)

    # Files domain — access (read) + share-fetch: "none" (default, off), "gdrive" (Google
    # Drive, reuses the gmail_* service account + drive.readonly scope) or "graph"
    # (Microsoft 365 OneDrive, reuses the graph_* app + Files.Read.All scope). Read-only.
    file_source_backend: str = "none"
    # Default-off write seam for the gdrive file source. When False (default) the runtime
    # files-access boundary is strictly read-only (drive.readonly): reads only, no Drive
    # writes. When True, create()/grant() use a separate full-`drive` service so consent-
    # gated writes work — PR.SV (file a report to Drive) and SH.G (grant a share link). Reads
    # stay read-only either way. Showcase sets this on; prod stays off. Env:
    # NORA_FILE_SOURCE_WRITABLE. (The SA's DWD must include the full `drive` scope.)
    file_source_writable: bool = False
    # R3 in-boundary file access: an absolute path to a directory Nora may read files from
    # for R3/keep-local relationships (path-contained). Empty = off. Files read here never
    # leave the boundary (the local model drafts; nothing egresses). Env: NORA_FILEVAULT_ROOT.
    filevault_root: str = ""

    # Cross-thread relationship memory (Layer A, vNext spike): "none" (default, off) or
    # "file" (per-CEO metadata-only breadcrumbs — counterparty + count + last-seen; NO
    # content). Only ever recorded/recalled on the R1 path. Env: NORA_MEMORY_BACKEND.
    memory_backend: str = "none"
    memory_path: str = ".nora-memory.json"
    # Layer C v2 (auto-learn): when on, Nora derives durable relationship facts from thread
    # content on the R1 draft path and folds them into future drafts (separate from CEO-curated
    # facts). Default off; independent of memory_backend. R1-only, fail-closed. Env:
    # NORA_RELATIONSHIP_AUTOLEARN.
    relationship_autolearn: bool = False
    # Ambient auto-draft (connected mailbox): when on, Nora polls the connected inbox and saves a
    # reply DRAFT (never sends) for genuine person-to-person mail into the owner's Drafts. Default
    # off; posture-gated to R1; idempotent via a label. Needs NORA_TRANSPORT=gmail + credentials.
    # Env: NORA_AUTODRAFT.
    autodraft: bool = False
    # Gmail label the poller stamps on every message it has PROCESSED — for idempotency, so a
    # message is never reconsidered. It means "seen", NOT "a draft was written": bulk/no-reply mail
    # the filter SKIPS is labelled too (no draft). Actual drafts live in the Drafts folder. Never
    # marks mail read. Env: NORA_AUTODRAFT_LABEL.
    autodraft_label: str = "Nora/seen"
    # Inbox actions: when on, a message forwarded to <local>+<command_alias>@ with an instruction
    # like "star and archive" makes Nora apply those SAFE actions to the original in the mailbox
    # (star/archive/label/read/important — never trash/delete). Default off; rides the poller.
    # Env: NORA_INBOX_ACTIONS / NORA_COMMAND_ALIAS.
    inbox_actions: bool = False
    command_alias: str = "nora"
    # Per-sender inbox-action rules: a command auto-becomes a rule (unless "once"); future mail
    # from that sender gets the same actions + a visible Nora/filed label; an archive-rule
    # suppresses the auto-draft. Env: NORA_INBOX_RULES_PATH / NORA_FILED_LABEL.
    inbox_rules_path: str = ".nora-inbox-rules.json"
    filed_label: str = "Nora/filed"
    # Draft-feedback learning: if the CEO deletes an auto-draft (doesn't send it), that's a negative
    # signal; after 2 deletions Nora stops auto-drafting that sender (a sent draft re-enables). Only
    # ever reduces drafting; reversible. Default off. Env: NORA_DRAFT_LEARNING / NORA_DRAFT_FEEDBACK_PATH.
    draft_learning: bool = False
    draft_feedback_path: str = ".nora-draft-feedback.json"

    # Per-mailbox reply-From domain override (reply-as-client-domain). Maps a registry
    # key (secret-address local part) -> the domain Nora's reply From uses, e.g.
    # {"nora.bennett": "example.nl"} makes that mailbox reply from
    # nora.bennett@example.nl instead of <key>@address_domain. REQUIRES the outbound
    # provider (Resend) to be verified for that domain (its DKIM/SPF DNS records on the
    # client domain) or the reply will fail DMARC. Unmatched keys fall back to
    # address_domain (no change). Reply-To stays on address_domain so the revise loop
    # needs no extra client-side routing. Default {} => no behavior change. Env:
    # NORA_SENDER_DOMAIN_OVERRIDES (JSON).
    sender_domain_overrides: dict[str, str] = Field(default_factory=dict)

    # Per-context posture (STRATEGY: Per-Context Posture). Maps a counterparty address or
    # domain to a rung ("R1"/"R2"/"R3"); unmatched = R1, exact address beats domain. A
    # relationship above R1 is declined on this version's external path (its content is not
    # sent to the LLM). Default {} => everything R1 (no behavior change). Env: NORA_POSTURE_MAP (JSON).
    posture_map: dict[str, str] = Field(default_factory=dict)

    # R1 self-serve onboarding (default-off: an empty allowlist admits no one).
    signup_local: str = "signup"
    # NoDecode: pydantic-settings JSON-decodes list fields from the env/dotenv source BEFORE
    # field validators run, so a bare CSV string ("a@x.com,@y.com") would fail to parse and crash
    # boot. NoDecode hands the raw string to _parse_allowlist below, which accepts CSV or JSON.
    signup_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    users_path: str = ".nora-users.json"

    @field_validator("ceo_registry", "sender_domain_overrides", mode="before")
    @classmethod
    def _parse_registry(cls, value: object) -> object:
        """Allow the dict to be supplied as a JSON string in the environment."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return {}
            return json.loads(value)
        return value

    @field_validator("signup_allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, value: object) -> object:
        """Allow the allowlist as a JSON array or a comma-separated string in the environment."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    def _needs_anthropic_key(self) -> bool:
        """True when a selected feature actually calls Claude: R1 drafting, the LLM
        interpreter, or web research. False ⇒ Nora runs Anthropic-key-free (e.g. fully on
        OpenRouter/Ollama)."""
        return (
            self.drafting_backend == "anthropic"
            or self.nl_interpreter_backend == "llm"
            or self.research_backend in ("claude", "deep")
        )

    def require_bootable(self) -> None:
        """Raise if the service is not safe to start.

        Enforces the operator acknowledgment (KTD5) and the presence of the
        credentials the service cannot function without.
        """
        missing: list[str] = []
        if not self.external_llm_ack:
            raise RuntimeError(
                "NORA_EXTERNAL_LLM_ACK is not set to true. In v1.0, forwarded thread "
                "content is processed by an external LLM; an operator must acknowledge "
                "this before the service will start (see KTD5)."
            )
        # The Anthropic key is required only when a feature actually uses Claude — drafting,
        # the LLM interpreter, or web research. With OpenRouter/Ollama backends and no Claude
        # research, Nora boots with no Anthropic key at all (external-LLM ack still applies —
        # OpenRouter is also an external LLM).
        if self._needs_anthropic_key() and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        # Self-serve onboarding: the signup local-part must not collide with a registered mailbox,
        # or signup mail would shadow that user (and that user could never reach normal auth).
        if self.signup_local and self.signup_local in self.ceo_registry:
            raise RuntimeError(
                f"NORA_SIGNUP_LOCAL '{self.signup_local}' collides with a NORA_CEO_REGISTRY key — "
                "choose a different signup local-part so signup mail can't shadow that mailbox."
            )
        # Webhook auth and the outbound From are required for every transport.
        if not self.webhook_basic_auth:
            missing.append("NORA_WEBHOOK_BASIC_AUTH")
        if not self.postmark_sender:
            missing.append("NORA_POSTMARK_SENDER")
        # Transport-specific credentials.
        if self.transport == "postmark":
            if not self.provider_api_key:
                missing.append("NORA_PROVIDER_API_KEY")
        elif self.transport == "resend":
            if not self.resend_api_key:
                missing.append("NORA_RESEND_API_KEY")
        elif self.transport == "smtp":
            if not self.smtp_host:
                missing.append("NORA_SMTP_HOST")
        elif self.transport == "gmail":
            # OAuth (personal Gmail) needs the OAuth client, not a service-account key.
            if self.gmail_auth == "oauth":
                if not self.gmail_oauth_client:
                    missing.append("NORA_GMAIL_OAUTH_CLIENT")
            elif not self.gmail_credentials_json:
                missing.append("NORA_GMAIL_CREDENTIALS_JSON")
            if not self.gmail_tenant_user:
                missing.append("NORA_GMAIL_TENANT_USER")
        else:
            raise RuntimeError(
                f"Unknown NORA_TRANSPORT '{self.transport}' "
                "(expected 'resend', 'postmark', 'smtp', or 'gmail')."
            )
        # Dev-only guard: a non-Claude backend must never handle real mail. Prod
        # transports (resend/postmark) require the anthropic backend until a local
        # model is eval-proven and deliberately promoted (future R3 step).
        if self.drafting_backend not in ("anthropic", "ollama", "openrouter"):
            raise RuntimeError(
                f"Unknown NORA_DRAFTING_BACKEND '{self.drafting_backend}' "
                "(expected 'anthropic', 'ollama', or 'openrouter')."
            )
        if self.drafting_backend != "anthropic" and self.transport in ("resend", "postmark"):
            raise RuntimeError(
                f"NORA_DRAFTING_BACKEND='{self.drafting_backend}' with a production transport "
                f"('{self.transport}'): the local/dev backend must not handle real mail. Use a "
                "dev transport (smtp), or set drafting_backend=anthropic."
            )
        # OpenRouter (drafting or interpreter) needs its key, or the backend silently fails.
        if "openrouter" in (self.drafting_backend, self.nl_interpreter_backend) and not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if self.nl_interpreter_backend not in ("keyword", "llm", "openrouter"):
            raise RuntimeError(
                f"Unknown NORA_NL_INTERPRETER_BACKEND '{self.nl_interpreter_backend}' "
                "(expected 'keyword', 'llm', or 'openrouter')."
            )
        # Fail loud on an unknown local backend rather than silently disabling KL/R3:
        # a typo (e.g. "olama") would otherwise leave local_drafting None, so every KL
        # email silently declines (fail-closed, but mystifying). Catch it at boot.
        if self.local_drafting_backend not in ("none", "ollama"):
            raise RuntimeError(
                f"Unknown NORA_LOCAL_DRAFTING_BACKEND '{self.local_drafting_backend}' "
                "(expected 'none' or 'ollama')."
            )
        # Optional backends: fail fast if half-configured (a typo or a missing credential
        # would otherwise silently leave the backend off — fail-closed, but mystifying).
        if self.calendar_backend not in ("none", "google"):
            raise RuntimeError(
                f"Unknown NORA_CALENDAR_BACKEND '{self.calendar_backend}' (expected 'none' or 'google')."
            )
        if self.calendar_backend == "google" and not self.google_credentials_json:
            missing.append("NORA_GOOGLE_CREDENTIALS_JSON")
        if self.research_backend not in ("none", "claude", "deep"):
            raise RuntimeError(
                f"Unknown NORA_RESEARCH_BACKEND '{self.research_backend}' "
                "(expected 'none', 'claude', or 'deep')."
            )
        # (Anthropic-for-research is folded into _needs_anthropic_key above.)
        if self.tenant_backend not in ("none", "gmail", "graph"):
            raise RuntimeError(
                f"Unknown NORA_TENANT_BACKEND '{self.tenant_backend}' "
                "(expected 'none', 'gmail', or 'graph')."
            )
        if self.tenant_backend == "gmail":
            if not self.gmail_credentials_json:
                missing.append("NORA_GMAIL_CREDENTIALS_JSON")
            if not self.gmail_tenant_user:
                missing.append("NORA_GMAIL_TENANT_USER")
        if self.tenant_backend == "graph":
            for name, val in (
                ("NORA_GRAPH_TENANT_ID", self.graph_tenant_id),
                ("NORA_GRAPH_CLIENT_ID", self.graph_client_id),
                ("NORA_GRAPH_CLIENT_SECRET", self.graph_client_secret),
                ("NORA_GRAPH_TENANT_USER", self.graph_tenant_user),
            ):
                if not val:
                    missing.append(name)
        if self.memory_backend not in ("none", "file"):
            raise RuntimeError(
                f"Unknown NORA_MEMORY_BACKEND '{self.memory_backend}' (expected 'none' or 'file')."
            )
        if self.file_source_backend not in ("none", "gdrive", "graph"):
            raise RuntimeError(
                f"Unknown NORA_FILE_SOURCE_BACKEND '{self.file_source_backend}' "
                "(expected 'none', 'gdrive', or 'graph')."
            )
        if self.file_source_backend == "gdrive":
            if not self.gmail_credentials_json:
                missing.append("NORA_GMAIL_CREDENTIALS_JSON")
            if not self.gmail_tenant_user:
                missing.append("NORA_GMAIL_TENANT_USER")
        if self.file_source_backend == "graph":
            for name, val in (
                ("NORA_GRAPH_TENANT_ID", self.graph_tenant_id),
                ("NORA_GRAPH_CLIENT_ID", self.graph_client_id),
                ("NORA_GRAPH_CLIENT_SECRET", self.graph_client_secret),
                ("NORA_GRAPH_TENANT_USER", self.graph_tenant_user),
            ):
                if not val:
                    missing.append(name)
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
