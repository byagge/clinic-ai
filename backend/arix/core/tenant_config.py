"""Typed view over ``tenant_config.data``.

Everything a clinic can change without a deploy lives here: tone of voice,
working hours, reminder offsets, feature flags, review links, texts.  The YAML
templates in ``arix/seed/templates`` are validated against this model, so a
malformed template fails at import time rather than mid-dialogue.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timeutil import QuietHours


class WorkingHours(BaseModel):
    """``{"mon-sat": "09:00-20:00", "sun": "off"}``"""

    model_config = ConfigDict(extra="allow")

    spec: dict[str, str] = Field(default_factory=lambda: {"mon-sun": "09:00-20:00"})

    @classmethod
    def from_raw(cls, raw: Any) -> WorkingHours:
        if isinstance(raw, dict):
            return cls(spec={str(k): str(v) for k, v in raw.items()})
        return cls()


class FeatureFlags(BaseModel):
    model_config = ConfigDict(extra="allow")

    followup: bool = True
    recall: bool = True
    reviews: bool = True
    waitlist: bool = False
    miniapp_payments: bool = False
    treatment_plans: bool = False
    instagram: bool = False
    voice_stt: bool = True
    doctor_bot: bool = True
    # edu-only flags are accepted here too so one model serves both verticals
    level_test: bool = True
    attendance: bool = True
    parent_reports: bool = False
    referral: bool = False


class ReminderConfig(BaseModel):
    """Offsets in hours relative to the appointment start (negative = before)."""

    visit_offsets_h: list[float] = Field(default_factory=lambda: [-24, -2])
    #: When a −24h reminder is unanswered, create a "call the patient" task.
    call_task_before_h: float = 3
    #: −2h reminders for early visits are pushed to this local time instead of
    #: firing inside quiet hours.
    early_morning_send_at: str = "08:00"


class FollowupConfig(BaseModel):
    #: Hours after an unconverted interest.
    offsets_h: list[float] = Field(default_factory=lambda: [1, 24, 72])
    max_touches: int = 3


class RecallConfig(BaseModel):
    lead_days: int = 7
    #: "Haven't seen you in a while" window, in months.
    dormant_months_from: int = 9
    dormant_months_to: int = 12
    dormant_max_per_month: int = 1


class ReviewConfig(BaseModel):
    delay_h: float = 2
    promote_from_score: int = 5
    alert_up_to_score: int = 3
    links: dict[str, str] = Field(default_factory=dict)


class HandoffConfig(BaseModel):
    pickup_minutes: int = 10
    escalate_minutes: int = 30
    #: Reasons that always page the owner immediately.
    urgent_reasons: list[str] = Field(
        default_factory=lambda: ["red_flag", "complaint", "refund"]
    )


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    #: Short tone instruction appended to the system prompt.
    tone: str = "коротко, тепло, по делу, без канцелярита и эмодзи-спама"
    #: ``/pause_bot`` in the owner bot flips this; the bot stores messages and
    #: pages the humans instead of answering.
    paused: bool = False
    #: Hard cap on consecutive misunderstood messages before handoff.
    max_confusion: int = 2
    slots_to_offer: int = 3
    greeting: str | None = None
    signature: str | None = None
    #: Extra instructions from the clinic (never allowed to relax guardrails).
    extra_rules: list[str] = Field(default_factory=list)


class BillingConfig(BaseModel):
    """edu vertical; harmless for clinics."""

    mode: Literal["monthly", "per_lesson", "course"] = "monthly"
    billing_day: str = "by_start"
    ladder_days: list[int] = Field(default_factory=lambda: [-3, 0, 3, 7, 10])


class TrialConfig(BaseModel):
    free: bool = True
    followup_hours: list[float] = Field(default_factory=lambda: [2, 24, 72])


class TenantSettings(BaseModel):
    """Root config object. Unknown keys are preserved, never rejected."""

    model_config = ConfigDict(extra="allow")

    display_name: str | None = None
    address: str | None = None
    map_url: str | None = None
    phone: str | None = None
    currency: str = "KZT"
    working_hours: dict[str, str] = Field(default_factory=lambda: {"mon-sun": "09:00-20:00"})
    quiet_hours: str | None = "21:00-09:00"
    agent: AgentConfig = Field(default_factory=AgentConfig)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    reminders: ReminderConfig = Field(default_factory=ReminderConfig)
    followup: FollowupConfig = Field(default_factory=FollowupConfig)
    recall: RecallConfig = Field(default_factory=RecallConfig)
    reviews: ReviewConfig = Field(default_factory=ReviewConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    billing: BillingConfig = Field(default_factory=BillingConfig)
    trial: TrialConfig = Field(default_factory=TrialConfig)
    owner_report_at: str = "08:00"
    #: Texts keyed by template code then language: ``{"reminder_24h": {"ru": ...}}``
    texts: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: Red-flag keywords that force an urgent handoff, per language.
    red_flags: dict[str, list[str]] = Field(default_factory=dict)
    consent_text: dict[str, str] = Field(default_factory=dict)
    disclaimer: dict[str, str] = Field(default_factory=dict)

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet(cls, v: str | None) -> str | None:
        if v and QuietHours.parse(v) is None:
            raise ValueError(f"invalid quiet_hours: {v!r}")
        return v

    @property
    def quiet(self) -> QuietHours | None:
        return QuietHours.parse(self.quiet_hours)

    def text(self, code: str, lang: str, default: str = "") -> str:
        bucket = self.texts.get(code, {})
        return bucket.get(lang) or bucket.get("ru") or default

    @classmethod
    def from_data(cls, data: dict[str, Any] | None) -> TenantSettings:
        return cls.model_validate(data or {})


__all__ = [
    "AgentConfig",
    "BillingConfig",
    "FeatureFlags",
    "FollowupConfig",
    "HandoffConfig",
    "RecallConfig",
    "ReminderConfig",
    "ReviewConfig",
    "TenantSettings",
    "TrialConfig",
    "WorkingHours",
]
