"""
The 3-way match decision, implemented exactly as docs/3WAY_MATCH_POLICY.md
specifies. This module is the ONLY implementation -- match-worker and the
seeder both import it, so the locked policy can never drift into two versions.

Core principle, non-negotiable: the pay/don't-pay decision is deterministic
and auditable, never an LLM judgment call. There is no model call anywhere in
this file. AI's role is around this engine (OCR extraction, anomaly flagging,
explanation), never inside it.

Pure functions only -- no database, no network. That is what makes the policy
directly unit-testable against the seeded clean/mismatched pairs.
"""

from dataclasses import dataclass, field
from decimal import Decimal

# Locked tolerances (3WAY_MATCH_POLICY.md).
# Quantity is tight: quantity mismatches are usually real errors, not rounding.
# Price is looser: covers rounding, FX, minor negotiated adjustments.
QTY_TOLERANCE = Decimal("0.02")    # 2%
PRICE_TOLERANCE = Decimal("0.03")  # 3%

APPROVED = "APPROVED"
EXCEPTION = "EXCEPTION"

QTY_MISMATCH = "QTY_MISMATCH"
PRICE_MISMATCH = "PRICE_MISMATCH"
MISSING_PO = "MISSING_PO"
DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
OTHER = "OTHER"

# Severity is not in the locked policy -- it is a presentation concern for the
# Exceptions Command Center. Kept here so the mapping lives beside the types it
# describes rather than being reinvented in the gateway.
SEVERITY_BY_TYPE = {
    MISSING_PO: "high",
    DUPLICATE_INVOICE: "critical",
    PRICE_MISMATCH: "high",
    QTY_MISMATCH: "medium",
    OTHER: "medium",
}


@dataclass
class MatchDecision:
    status: str                       # APPROVED | EXCEPTION
    exception_type: str | None        # None when APPROVED
    reason: str                       # always states the actual numbers
    severity: str | None = None
    impact_amount: Decimal | None = None
    qty_variance_pct: Decimal | None = None
    price_variance_pct: Decimal | None = None
    checks: list[dict] = field(default_factory=list)  # ordered audit trail

    @property
    def approved(self) -> bool:
        return self.status == APPROVED


def _d(value) -> Decimal:
    """Coerce to Decimal. Money and quantities never touch float."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def evaluate(
    *,
    po_id: str | None,
    po_status: str,
    po_qty,
    po_unit_price,
    qty_received,
    qty_invoiced,
    unit_price_invoiced,
) -> MatchDecision:
    """
    Run the locked decision sequence. Steps are evaluated in the exact order
    given in 3WAY_MATCH_POLICY.md -- the order is part of the contract, not an
    implementation detail. Steps 3 and 4 guard the divisions in steps 5 and 6
    and are checked BEFORE the division, never after.

    Quantity is checked against what was RECEIVED (goods_receipts), not what
    was ordered: the PO authorizes, the receipt confirms physical reality, the
    invoice bills against that reality. Price is checked against the PO, since
    receipts carry no price.
    """
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    # ---- Hard rules, before any tolerance math ----

    # 1. Invoice with no PO reference. po_id is nullable in the locked schema
    #    specifically so this scenario is representable.
    if po_id is None:
        record("po_reference", False, "invoice carries no PO reference")
        return MatchDecision(
            status=EXCEPTION,
            exception_type=MISSING_PO,
            reason="invoice has no PO reference; cannot 3-way match",
            severity=SEVERITY_BY_TYPE[MISSING_PO],
            checks=checks,
        )
    record("po_reference", True, f"invoice references {po_id}")

    # 2. Already-settled PO. Prevents paying the same PO twice.
    if po_status in ("MATCHED", "CLOSED"):
        record("not_already_settled", False, f"{po_id} status is {po_status}")
        return MatchDecision(
            status=EXCEPTION,
            exception_type=DUPLICATE_INVOICE,
            reason=f"{po_id} is already {po_status}; this invoice would pay it twice",
            severity=SEVERITY_BY_TYPE[DUPLICATE_INVOICE],
            checks=checks,
        )
    record("not_already_settled", True, f"{po_id} status is {po_status}")

    qty_received = _d(qty_received)
    qty_invoiced = _d(qty_invoiced)
    po_unit_price = _d(po_unit_price)
    unit_price_invoiced = _d(unit_price_invoiced)
    po_qty = _d(po_qty)

    # 3. Guard the quantity division. A zero/negative receipt is a data
    #    problem, not a quantity variance -- never divide by it.
    if qty_received <= 0:
        record("receipt_quantity_positive", False, f"qty_received = {qty_received}")
        return MatchDecision(
            status=EXCEPTION,
            exception_type=OTHER,
            reason=f"goods receipt quantity is {qty_received}; cannot compute variance",
            severity=SEVERITY_BY_TYPE[OTHER],
            checks=checks,
        )
    record("receipt_quantity_positive", True, f"qty_received = {qty_received}")

    # 4. Guard the price division, same reasoning.
    if po_unit_price <= 0:
        record("po_price_positive", False, f"po.unit_price = {po_unit_price}")
        return MatchDecision(
            status=EXCEPTION,
            exception_type=OTHER,
            reason=f"PO unit price is {po_unit_price}; cannot compute variance",
            severity=SEVERITY_BY_TYPE[OTHER],
            checks=checks,
        )
    record("po_price_positive", True, f"po.unit_price = {po_unit_price}")

    # ---- Tolerance evaluation ----
    qty_variance = abs(qty_invoiced - qty_received) / qty_received
    price_variance = abs(unit_price_invoiced - po_unit_price) / po_unit_price

    impact = abs((qty_invoiced * unit_price_invoiced) - (po_qty * po_unit_price))

    # 5. Quantity variance.
    if qty_variance > QTY_TOLERANCE:
        record(
            "qty_within_tolerance",
            False,
            f"received {qty_received}, invoiced {qty_invoiced} -> {_pct(qty_variance)}",
        )
        return MatchDecision(
            status=EXCEPTION,
            exception_type=QTY_MISMATCH,
            reason=(
                f"qty variance {_pct(qty_variance)} "
                f"(received {qty_received}, invoiced {qty_invoiced}) "
                f"exceeds {_pct(QTY_TOLERANCE)} tolerance"
            ),
            severity=SEVERITY_BY_TYPE[QTY_MISMATCH],
            impact_amount=impact,
            qty_variance_pct=qty_variance,
            price_variance_pct=price_variance,
            checks=checks,
        )
    record(
        "qty_within_tolerance",
        True,
        f"received {qty_received}, invoiced {qty_invoiced} -> {_pct(qty_variance)}",
    )

    # 6. Price variance.
    if price_variance > PRICE_TOLERANCE:
        record(
            "price_within_tolerance",
            False,
            f"PO {po_unit_price}, invoiced {unit_price_invoiced} -> {_pct(price_variance)}",
        )
        return MatchDecision(
            status=EXCEPTION,
            exception_type=PRICE_MISMATCH,
            reason=(
                f"price variance {_pct(price_variance)} "
                f"(PO {po_unit_price}, invoiced {unit_price_invoiced}) "
                f"exceeds {_pct(PRICE_TOLERANCE)} tolerance"
            ),
            severity=SEVERITY_BY_TYPE[PRICE_MISMATCH],
            impact_amount=impact,
            qty_variance_pct=qty_variance,
            price_variance_pct=price_variance,
            checks=checks,
        )
    record(
        "price_within_tolerance",
        True,
        f"PO {po_unit_price}, invoiced {unit_price_invoiced} -> {_pct(price_variance)}",
    )

    # 7. Everything passed.
    return MatchDecision(
        status=APPROVED,
        exception_type=None,
        reason=(
            f"qty variance {_pct(qty_variance)} within {_pct(QTY_TOLERANCE)}, "
            f"price variance {_pct(price_variance)} within {_pct(PRICE_TOLERANCE)}"
        ),
        impact_amount=impact,
        qty_variance_pct=qty_variance,
        price_variance_pct=price_variance,
        checks=checks,
    )
