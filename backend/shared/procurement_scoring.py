"""
Supplier selection scoring. Deterministic and auditable -- the model does not
choose the supplier, it only narrates the result (see shared/llm.py).

Every candidate is scored and persisted, not just the winner. That is what lets
the demo answer "why Supplier B?" with the actual numbers rather than a claim.
"""

from dataclasses import dataclass
from decimal import Decimal

# Weights sum to 1.0. Price dominates slightly, then quality; risk is the
# smallest term because it is already partly reflected in reliability.
W_PRICE = 0.30
W_QUALITY = 0.25
W_LEAD_TIME = 0.20
W_RELIABILITY = 0.15
W_RISK = 0.10

# Lead time is normalised against this horizon: a supplier at or beyond it
# scores 0 on speed.
LEAD_TIME_HORIZON_DAYS = 10


@dataclass
class ScoredSupplier:
    supplier_id: str
    supplier_name: str
    price_score: float
    quality_score: float
    lead_time_score: float
    reliability_score: float
    risk_score: float
    overall_score: float
    quoted_unit_price: Decimal
    quoted_lead_time_days: float
    rank: int = 0
    recommended: bool = False

    def as_dict(self) -> dict:
        return {
            "supplier_id": self.supplier_id,
            "supplier_name": self.supplier_name,
            "price_score": self.price_score,
            "quality_score": self.quality_score,
            "lead_time_score": self.lead_time_score,
            "reliability_score": self.reliability_score,
            "risk_score": self.risk_score,
            "overall_score": self.overall_score,
            "quoted_unit_price": float(self.quoted_unit_price),
            "quoted_lead_time_days": self.quoted_lead_time_days,
            "rank": self.rank,
            "recommended": self.recommended,
        }


def score_suppliers(*, candidates: list[dict], base_price: float) -> list[ScoredSupplier]:
    """
    `candidates` are supplier rows: id, name, reliability_score,
    avg_lead_time_days, quality_score, risk_score, price_multiplier.

    Price is scored RELATIVE to the cheapest quote in this candidate set rather
    than against an absolute band, so the comparison stays meaningful whether
    the material costs 40 cents or 1200 dollars.
    """
    if not candidates:
        return []

    quotes = {
        c["id"]: (Decimal(str(base_price)) * Decimal(str(c.get("price_multiplier", 1.0)))
                  ).quantize(Decimal("0.01"))
        for c in candidates
    }
    cheapest = min(quotes.values())
    dearest = max(quotes.values())
    price_span = float(dearest - cheapest)

    scored: list[ScoredSupplier] = []
    for c in candidates:
        quote = quotes[c["id"]]
        # Flat price field carries no signal -> everyone gets full marks
        # rather than an arbitrary ordering.
        price_score = 1.0 if price_span == 0 else round(
            1.0 - (float(quote - cheapest) / price_span), 3
        )

        lead_days = float(c.get("avg_lead_time_days") or LEAD_TIME_HORIZON_DAYS)
        lead_score = round(max(0.0, 1.0 - min(lead_days, LEAD_TIME_HORIZON_DAYS)
                               / LEAD_TIME_HORIZON_DAYS), 3)

        quality = float(c.get("quality_score") or 0)
        reliability = float(c.get("reliability_score") or 0)
        risk = float(c.get("risk_score") or 0)

        overall = round(
            W_PRICE * price_score
            + W_QUALITY * quality
            + W_LEAD_TIME * lead_score
            + W_RELIABILITY * reliability
            + W_RISK * (1.0 - risk),
            3,
        )

        scored.append(ScoredSupplier(
            supplier_id=c["id"], supplier_name=c["name"],
            price_score=price_score, quality_score=round(quality, 3),
            lead_time_score=lead_score, reliability_score=round(reliability, 3),
            risk_score=round(risk, 3), overall_score=overall,
            quoted_unit_price=quote, quoted_lead_time_days=lead_days,
        ))

    scored.sort(key=lambda s: (-s.overall_score, s.quoted_unit_price))
    for i, s in enumerate(scored, start=1):
        s.rank = i
        s.recommended = i == 1
    return scored
