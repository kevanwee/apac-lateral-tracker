"""The extraction call and everything that decides whether to believe it."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

from tracker.extract import confidence
from tracker.extract.prompt import SYSTEM_PROMPT, build_user_message
from tracker.extract.schema import EXTRACTION_SCHEMA, SCHEMA_VERSION
from tracker.extract.spans import VerifiedField, verify
from tracker.sources.base import RawItem

log = logging.getLogger(__name__)

# Per 1M tokens. Update alongside the model default.
PRICING_USD = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Fields whose value is a code the source text will not contain literally.
# Their spans are checked for sanity but not string-matched.
CODED_FIELDS = {"move_type", "office_jurisdiction", "partner_tier"}

# Without these there is no record to store.
REQUIRED_FIELDS = {"person_name"}


class CostCeilingExceeded(RuntimeError):
    """Raised when a run would spend past its ceiling. Never truncates silently."""


@dataclass
class ExtractedMove:
    fields: dict[str, VerifiedField]
    dropped: list[str]
    team_size: int | None
    self_confidence: float
    confidence: confidence.ConfidenceComponents

    @property
    def person_name(self) -> str | None:
        f = self.fields.get("person_name")
        return f.value if f else None

    def value(self, name: str) -> str | None:
        f = self.fields.get(name)
        return f.value if f else None

    @property
    def needs_review(self) -> bool:
        return confidence.needs_review(self.confidence.total)


@dataclass
class ExtractionResult:
    item: RawItem
    is_movement: bool
    not_movement_reason: str | None
    moves: list[ExtractedMove] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    schema_version: str = SCHEMA_VERSION
    error: str | None = None


@dataclass
class CostLedger:
    """Enforces the per-run ceiling. Fails loudly rather than quietly stopping."""

    ceiling_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    def check_before(self, projected_usd: float) -> None:
        if self.spent_usd + projected_usd > self.ceiling_usd:
            raise CostCeilingExceeded(
                f"run would spend ${self.spent_usd + projected_usd:.4f}, "
                f"over the ${self.ceiling_usd:.2f} ceiling after {self.calls} calls. "
                f"Raise TRACKER_LLM_COST_CEILING_USD_PER_RUN or narrow the run."
            )

    def record(self, usd: float) -> None:
        self.spent_usd += usd
        self.calls += 1


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING_USD.get(model, PRICING_USD["claude-opus-5"])
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


class Extractor:
    """Turns one RawItem into zero or more verified movement records."""

    def __init__(
        self,
        *,
        model: str,
        client: anthropic.Anthropic | None = None,
        ledger: CostLedger | None = None,
    ) -> None:
        self.model = model
        self.client = client or anthropic.Anthropic()
        self.ledger = ledger

    def extract(self, item: RawItem, *, reliability_tier: int) -> ExtractionResult:
        text = item.extraction_text
        started = time.monotonic()

        if self.ledger is not None:
            # Rough projection: the prompt plus a full response. Deliberately
            # pessimistic, so the ceiling is hit before it is breached.
            projected = cost_of(self.model, len(text) // 3 + 1200, 1500)
            self.ledger.check_before(projected)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": build_user_message(
                            text,
                            source_name=item.source_slug,
                            access_level=item.access_level,
                        ),
                    }
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
                },
            )
        except anthropic.APIError as exc:
            return ExtractionResult(
                item=item.without_text(),
                is_movement=False,
                not_movement_reason=None,
                model=self.model,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = response.usage
        cost = cost_of(self.model, usage.input_tokens, usage.output_tokens)
        if self.ledger is not None:
            self.ledger.record(cost)

        result = ExtractionResult(
            item=item.without_text(),
            is_movement=False,
            not_movement_reason=None,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=self.model,
        )

        payload = self._payload(response)
        if payload is None:
            result.error = "model returned no parseable JSON payload"
            return result

        result.is_movement = bool(payload.get("is_movement"))
        result.not_movement_reason = payload.get("not_movement_reason")
        if not result.is_movement:
            return result

        for raw_move in payload.get("moves") or []:
            built = self._build_move(
                raw_move,
                text=text,
                reliability_tier=reliability_tier,
                access_level=item.access_level,
            )
            if built is not None:
                result.moves.append(built)

        return result

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _payload(response) -> dict | None:
        parsed = getattr(response, "parsed_output", None)
        if isinstance(parsed, dict):
            return parsed
        for block in response.content:
            if block.type == "text":
                try:
                    return json.loads(block.text)
                except json.JSONDecodeError:
                    log.warning("extraction returned non-JSON text")
                    return None
        return None

    @staticmethod
    def _build_move(
        raw: dict,
        *,
        text: str,
        reliability_tier: int,
        access_level: str,
    ) -> ExtractedMove | None:
        verified: dict[str, VerifiedField] = {}
        dropped: list[str] = []

        for name in (
            "person_name",
            "from_firm",
            "to_firm",
            "title_from",
            "title_to",
            "partner_tier",
            "office_jurisdiction",
            "practice_text",
            "sector_text",
            "effective_date_text",
            "move_type",
        ):
            checked = verify(
                name,
                raw.get(name),
                text,
                value_must_appear=name not in CODED_FIELDS,
            )
            if checked is None:
                continue
            if checked.kept:
                verified[name] = checked
            else:
                # The model asserted something the text does not contain. That
                # is the failure mode this whole layer exists to catch.
                dropped.append(name)
                log.info("dropped unsupported field %s=%r", name, checked.value)

        # A record with no verifiable person is not a record.
        if not verified.keys() >= REQUIRED_FIELDS:
            return None

        # A destination is required for everything but a retirement, and an
        # unverifiable destination is worse than no record.
        move_type = verified["move_type"].value if "move_type" in verified else None
        if "to_firm" not in verified and move_type != "retirement":
            return None

        components = confidence.score(
            reliability_tier=reliability_tier,
            access_level=access_level,
            present_fields=set(verified),
            span_qualities=[f.quality for f in verified.values()],
            self_reported=raw.get("self_confidence") or 0.0,
        )

        return ExtractedMove(
            fields=verified,
            dropped=dropped,
            team_size=raw.get("team_size"),
            self_confidence=raw.get("self_confidence") or 0.0,
            confidence=components,
        )
