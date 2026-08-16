#!/usr/bin/env python
"""
Eval harness (BUILD_PLAN.md §5.3). Measures what this system actually decided
against an answer key, and writes `backend/eval/eval_results.json` for
`GET /kpi/model-performance` to serve.

    ./.venv/bin/python backend/eval/run_eval.py             # free suites only
    ./.venv/bin/python backend/eval/run_eval.py --nlp       # + 30 live parse calls
    ./.venv/bin/python backend/eval/run_eval.py --all
    ./.venv/bin/python backend/eval/run_eval.py --out /tmp/eval.json

WHY THE ANSWER KEY IS `scenario` AND NOT `expected_match_status`
---------------------------------------------------------------
`seed/ground_truth.json` carries both. Only one of them is usable.

`expected_match_status` and `expected_exception_type` are written by seed.py
from `decision_m = evaluate(...)` -- the output of the very policy this suite
grades. Scoring the policy against them compares a function to itself and
returns F1 = 1.00 by construction, for every input, forever. That number would
be worse than no number: it looks like a measurement and is an identity.

`scenario` is decided BEFORE the policy runs. It is the fault the seeder chose
to inject -- how the invoice was perturbed -- and the outcome each fault should
produce is fixed by BUILD_PLAN.md §5.1, independently of any code. That mapping
is SCENARIO_EXPECTATION below, and it is the answer key.

So this suite asks the one question worth asking: when a known fault was
injected, did the system catch that fault, and when none was, did it stay
quiet? `near_miss_qty` is the case that earns the harness its keep -- a 1.5%
quantity variance that must be APPROVED. A policy that flagged every variance
would score perfectly on the other five scenarios and fail only here.

WHAT IS MEASURED, AND WHAT IS NOT
---------------------------------
  match_classifier  the 3-way match as a 5-class classifier, scored against
                    the injected scenario. Reads the database, no API calls.
  operational       first-pass rate, touchless rate, turnaround, P2P cycle,
                    exception mix, supplier acceptance. Read from
                    /dashboard/overview's own functions rather than re-queried
                    here, so the dashboard and this file can never disagree.
  nlp_parse         30 labelled phrasings through the live parser. OPT-IN
                    (--nlp): it costs real API calls, and a harness that
                    silently bills the user every run is a bad harness.
  ocr               field accuracy + confidence calibration. Reports
                    "not_measured" while no invoice has a stored document --
                    nothing in this system renders invoice images yet, so
                    there is nothing to read. An honest gap beats a fabricated
                    score (README §8).

Every suite records its own sample size. A rate over six exceptions and a rate
over six hundred are not the same claim.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from shared.db import get_conn  # noqa: E402

GROUND_TRUTH_PATH = BACKEND_ROOT / "seed" / "ground_truth.json"
REQUISITIONS_PATH = Path(__file__).resolve().parent / "requisitions.json"
DEFAULT_OUT_PATH = Path(__file__).resolve().parent / "eval_results.json"

# BUILD_PLAN.md §5.1's mismatch mix, as (expected_status, expected_exception).
# This is the answer key -- see the module docstring for why the ground-truth
# file's own expected_* fields are not.
SCENARIO_EXPECTATION = {
    "clean":             ("APPROVED", None),
    # A 1.5% quantity variance, inside the 2% tolerance. The whole point of the
    # case is that the correct answer is APPROVED despite a visible variance.
    "near_miss_qty":     ("APPROVED", None),
    "qty_mismatch":      ("EXCEPTION", "QTY_MISMATCH"),
    "price_mismatch":    ("EXCEPTION", "PRICE_MISMATCH"),
    "missing_po":        ("EXCEPTION", "MISSING_PO"),
    # The chain's PRIMARY invoice is legitimate and must be approved; the
    # duplicate is a second invoice row carried on `duplicate_invoice_id` and
    # is scored as its own case below.
    "duplicate_invoice": ("APPROVED", None),
}

CLASSES = ["APPROVED", "QTY_MISMATCH", "PRICE_MISMATCH", "MISSING_PO", "DUPLICATE_INVOICE"]


# ─────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────

def _prf(tp: int, fp: int, fn: int) -> dict:
    """
    Precision/recall/F1 for one class.

    A class with no predictions and no truths scores None, not 0.0. Zero is a
    claim that the classifier failed; None says it was never tested. Averaging
    an untested class in as 0.0 is the most common way a macro-F1 gets quietly
    understated.
    """
    if tp + fp + fn == 0:
        return {"precision": None, "recall": None, "f1": None,
                "support": 0, "tp": 0, "fp": 0, "fn": 0}
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4), "support": tp + fn, "tp": tp, "fp": fp, "fn": fn}


def _score(pairs: list[tuple[str, str]]) -> dict:
    """(expected, predicted) pairs -> confusion matrix + per-class + averages."""
    confusion = defaultdict(lambda: defaultdict(int))
    for expected, predicted in pairs:
        confusion[expected][predicted] += 1

    per_class = {}
    for cls in CLASSES:
        tp = confusion[cls][cls]
        fp = sum(confusion[other][cls] for other in CLASSES if other != cls)
        fn = sum(confusion[cls][other] for other in CLASSES if other != cls)
        per_class[cls] = _prf(tp, fp, fn)

    correct = sum(1 for e, p in pairs if e == p)
    tested = [c for c in CLASSES if per_class[c]["support"] or per_class[c]["fp"]]

    # Macro treats every fault type as equally important; weighted reflects the
    # mix actually seen. Both are reported because on a set that is 73% clean
    # they say very different things, and quoting only the flattering one is
    # how an eval stops being an eval.
    macro = [per_class[c]["f1"] for c in tested if per_class[c]["f1"] is not None]
    total_support = sum(per_class[c]["support"] for c in CLASSES)
    weighted = sum(
        per_class[c]["f1"] * per_class[c]["support"]
        for c in CLASSES if per_class[c]["f1"] is not None
    ) / total_support if total_support else None

    return {
        "n": len(pairs),
        "accuracy": round(correct / len(pairs), 4) if pairs else None,
        "correct": correct,
        "incorrect": len(pairs) - correct,
        "macro_f1": round(sum(macro) / len(macro), 4) if macro else None,
        "weighted_f1": round(weighted, 4) if weighted is not None else None,
        "classes_observed": tested,
        "per_class": per_class,
        "confusion_matrix": {e: {p: confusion[e][p] for p in CLASSES} for e in CLASSES},
    }


# ─────────────────────────────────────────────
# Suite 1 -- the 3-way match as a classifier
# ─────────────────────────────────────────────

def _actual_label(cur, invoice_id: str):
    """
    What the SYSTEM decided about this invoice, as one of CLASSES.

    Returns None when the invoice was never matched -- an unmatched invoice is
    not a wrong answer, it is an absent one, and it belongs in `unmatched`
    rather than in a confusion matrix cell.
    """
    cur.execute(
        """SELECT mr.status, e.exception_type
           FROM match_results mr
           LEFT JOIN exceptions e ON e.match_result_id = mr.id
           WHERE mr.invoice_id = %s""",
        (invoice_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    status, exception_type = row
    if status == "APPROVED":
        return "APPROVED"
    # An EXCEPTION whose type is missing would silently vanish from the matrix,
    # so it is reported as OTHER and will show up as a wrong answer.
    return exception_type or "OTHER"


def run_match_classifier(cur, chains) -> dict:
    pairs, unmatched, unknown_scenario, cases = [], [], [], []

    for chain in chains:
        scenario = chain.get("scenario")
        if scenario not in SCENARIO_EXPECTATION:
            unknown_scenario.append({"po_id": chain.get("po_id"), "scenario": scenario})
            continue

        expected_status, expected_exception = SCENARIO_EXPECTATION[scenario]
        expected = "APPROVED" if expected_status == "APPROVED" else expected_exception

        for invoice_id, label in (
            (chain.get("invoice_id"), expected),
            # The injected duplicate, scored on its own. It is not a chain row,
            # so without this the one scenario the mix names "duplicate_invoice"
            # would never actually test DUPLICATE_INVOICE detection.
            (chain.get("duplicate_invoice_id"), "DUPLICATE_INVOICE"),
        ):
            if not invoice_id:
                continue
            predicted = _actual_label(cur, invoice_id)
            if predicted is None:
                unmatched.append({"po_id": chain.get("po_id"), "invoice_id": invoice_id,
                                  "scenario": scenario, "stage": chain.get("stage")})
                continue
            pairs.append((label, predicted))
            cases.append({"invoice_id": invoice_id, "po_id": chain.get("po_id"),
                          "scenario": scenario, "expected": label, "predicted": predicted,
                          "correct": label == predicted})

    result = _score(pairs)
    result["answer_key"] = "seed/ground_truth.json -> chains[].scenario, mapped by BUILD_PLAN.md §5.1"
    result["answer_key_note"] = (
        "ground_truth.json's own expected_match_status is NOT used: seed.py writes it "
        "from the same evaluate() call this suite grades, so scoring against it would "
        "return F1=1.00 by construction."
    )
    result["scenario_mix"] = dict(Counter(c["scenario"] for c in cases))

    # How well a classifier with no logic at all would do on this exact set.
    # The mix is ~74% clean, so "always APPROVED" already scores 0.74, and an
    # accuracy of 1.00 quoted without that number sounds far more impressive
    # than it is. Reporting the floor next to the score is the difference
    # between a measurement and a boast.
    expected_counts = Counter(expected for expected, _ in pairs)
    result["majority_class_baseline"] = {
        "label": expected_counts.most_common(1)[0][0] if expected_counts else None,
        "accuracy": (round(expected_counts.most_common(1)[0][1] / len(pairs), 4)
                     if pairs else None),
        "note": "accuracy of a classifier that always predicts the most common label. "
                "The score above is only meaningful to the extent it beats this.",
    }

    # The near-miss is the one case in the mix that separates a tolerance
    # policy from a rule that flags every variance: a 1.5% quantity difference
    # that must still be APPROVED. Called out by name because it is a single
    # sample and therefore invisible in an aggregate.
    result["discriminating_cases"] = [
        {k: c[k] for k in ("invoice_id", "scenario", "expected", "predicted", "correct")}
        for c in cases if c["scenario"] == "near_miss_qty"
    ]
    result["unmatched_invoices"] = len(unmatched)
    result["unmatched_detail"] = unmatched[:20]
    result["unknown_scenarios"] = unknown_scenario
    result["misclassified"] = [c for c in cases if not c["correct"]]
    return result


# ─────────────────────────────────────────────
# Suite 2 -- operational KPIs
# ─────────────────────────────────────────────

def run_operational(cur) -> dict:
    """
    The running system's own KPI numbers, captured as a point-in-time snapshot.

    These are READ FROM the gateway's own overview() rather than re-queried
    here. Every one of them (first-pass rate, touchless rate, turnaround, P2P
    cycle) has a carefully-chosen denominator documented at its query, and a
    second implementation in this file would drift from it within one change --
    at which point the dashboard and the eval file would report two different
    "touchless rates" and both would look wrong.
    """
    import services.dashboard_gateway.main as gw

    overview = gw.overview()

    cur.execute("""SELECT e.exception_type, e.severity, count(*)
                   FROM exceptions e GROUP BY 1, 2 ORDER BY 3 DESC""")
    breakdown = [{"exception_type": t, "severity": s, "count": n} for t, s, n in cur.fetchall()]

    cur.execute("SELECT count(*) FILTER (WHERE resolved_at IS NOT NULL), count(*) FROM exceptions")
    resolved, total_exceptions = cur.fetchone()

    # Supplier acceptance: of the POs put to a supplier, the share the supplier
    # confirmed. CREATED is "sent, not yet answered" -- it is the denominator,
    # not a rejection -- so this rises as confirmations arrive rather than
    # starting at 100% and decaying.
    cur.execute("""
        SELECT count(*) FILTER (WHERE status <> 'CREATED'), count(*)
        FROM purchase_orders
    """)
    accepted, po_total = cur.fetchone()

    return {
        "kpis": overview["kpis"],
        "kpi_basis": overview["kpi_basis"],
        "source": "GET /dashboard/overview (same functions, not a re-implementation)",
        "exception_breakdown": breakdown,
        "exceptions_total": total_exceptions,
        "exceptions_resolved": resolved,
        "supplier_acceptance_rate": round(accepted / po_total, 4) if po_total else None,
        "supplier_acceptance_basis": {"confirmed_or_beyond": accepted, "purchase_orders": po_total},
    }


# ─────────────────────────────────────────────
# Suite 3 -- NLP parse (opt-in; live API calls)
# ─────────────────────────────────────────────

def run_nlp(cur) -> dict:
    from shared.llm import ai_available, ai_provider, parse_requisition

    fixture = json.loads(REQUISITIONS_PATH.read_text())
    cases = fixture["cases"]

    if not ai_available():
        return {"status": "not_measured",
                "reason": "no ANTHROPIC_API_KEY or OPENAI_API_KEY configured; the parser "
                          "would run in deterministic fallback mode and the score would "
                          "measure the stub, not the model.",
                "cases_available": len(cases)}

    cur.execute("SELECT id, name, uom FROM materials ORDER BY id")
    materials = [{"id": r[0], "name": r[1], "uom": r[2]} for r in cur.fetchall()]
    cur.execute("SELECT id, name FROM locations ORDER BY id")
    locations = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    today = datetime.now(timezone.utc).date().isoformat()

    # Scored per FIELD, not per sentence: a parse that gets the material right
    # and the quantity wrong is not simply "wrong", and one number that hides
    # which half failed is not useful for fixing anything.
    field_hits = Counter()
    field_total = Counter()
    results, failures = [], []

    for case in cases:
        parsed, used_ai = parse_requisition(
            case["text"], materials=materials, locations=locations, today=today,
        )
        record = {"id": case["id"], "text": case["text"], "used_ai": used_ai}

        if case.get("expect_ambiguous"):
            # Correct behaviour is to decline to invent the missing field.
            flagged = bool(parsed.ambiguities) or parsed.material_id is None or parsed.qty == 0
            field_total["ambiguity_flagged"] += 1
            field_hits["ambiguity_flagged"] += int(flagged)
            record.update({"kind": "ambiguous", "correct": flagged,
                           "ambiguities": parsed.ambiguities,
                           "material_id": parsed.material_id, "qty": parsed.qty})
            if not flagged:
                failures.append(record)
            results.append(record)
            continue

        record["kind"] = "specific"
        record["fields"] = {}
        for field, expected in case["expect"].items():
            actual = getattr(parsed, field, None)
            if field == "qty":
                hit = actual is not None and abs(float(actual) - float(expected)) < 1e-6
            elif field == "uom":
                hit = (actual or "").strip().lower() == str(expected).lower()
            else:
                hit = actual == expected
            field_total[field] += 1
            field_hits[field] += int(hit)
            record["fields"][field] = {"expected": expected, "actual": actual, "correct": bool(hit)}
        record["correct"] = all(f["correct"] for f in record["fields"].values())
        if not record["correct"]:
            failures.append(record)
        results.append(record)

    per_field = {
        field: {"accuracy": round(field_hits[field] / field_total[field], 4),
                "correct": field_hits[field], "n": field_total[field]}
        for field in sorted(field_total)
    }
    graded = sum(field_total.values())
    exact = sum(1 for r in results if r["correct"])

    # Load-bearing, not bookkeeping. parse_requisition() degrades to a
    # deterministic stub on any per-call failure, and the stub is good enough
    # to pass some of these cases. If even one case fell back, part of this
    # score measures the fallback rather than the model, and the number must
    # say so instead of quietly averaging the two together.
    used_ai_cases = sum(1 for r in results if r["used_ai"])

    return {
        "status": "measured",
        "provider": ai_provider(),
        "cases": len(cases),
        "cases_via_live_model": used_ai_cases,
        "cases_via_fallback_stub": len(cases) - used_ai_cases,
        "score_is_purely_the_model": used_ai_cases == len(cases),
        "exact_match_cases": exact,
        "exact_match_rate": round(exact / len(cases), 4) if cases else None,
        "field_accuracy": round(sum(field_hits.values()) / graded, 4) if graded else None,
        "fields_graded": graded,
        "per_field": per_field,
        "answer_key": "backend/eval/requisitions.json (hand-labelled; not seeded text)",
        "failures": failures,
    }


# ─────────────────────────────────────────────
# Suite 4 -- OCR
# ─────────────────────────────────────────────

def run_ocr(cur) -> dict:
    """
    Field accuracy and confidence calibration over invoice scans.

    There are none. `invoices.document_path` is written only by Procurement
    API's image-upload path, and nothing in the seed or the simulator renders
    an invoice image -- every invoice in this system arrived as structured
    JSON. `ocr_raw` and `ocr_confidence` on those rows were written by the
    seeder, so scoring them would grade the seeder's random.uniform() call.

    Reported as an explicit gap rather than omitted or faked, on the same
    principle that makes GET /kpi/model-performance 404 before a first run.
    """
    cur.execute("SELECT count(*) FILTER (WHERE document_path IS NOT NULL), count(*) FROM invoices")
    with_document, total = cur.fetchone()

    if not with_document:
        return {
            "status": "not_measured",
            "reason": "no invoice in the database has a stored document. Nothing renders "
                      "invoice images yet, so there is no scan to read and no field to "
                      "score. ocr_raw on the seeded rows was written by seed.py, not by "
                      "an OCR run, and grading it would measure the seeder.",
            "invoices_total": total,
            "invoices_with_document": 0,
            "to_enable": "upload invoice images via POST /invoices/upload, then re-run with --ocr",
        }

    return {
        "status": "not_implemented",
        "reason": f"{with_document} invoice document(s) are on disk, but this suite needs a "
                  "per-field answer key for each scan before it can score extraction or "
                  "calibration. Add one alongside the images and implement here.",
        "invoices_total": total,
        "invoices_with_document": with_document,
    }


# ─────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────

def _print_report(results) -> None:
    m = results["suites"]["match_classifier"]
    print("\n" + "=" * 66)
    print("  3-WAY MATCH AS A CLASSIFIER")
    print("=" * 66)
    print(f"  answer key : {m['answer_key']}")
    print(f"  invoices   : {m['n']}  ({m['correct']} correct, {m['incorrect']} wrong)")
    base = m["majority_class_baseline"]
    print(f"  accuracy   : {m['accuracy']}   "
          f"(always-{base['label']} baseline would score {base['accuracy']})")
    print(f"  macro F1   : {m['macro_f1']}      weighted F1: {m['weighted_f1']}")
    if m["unmatched_invoices"]:
        print(f"  not scored : {m['unmatched_invoices']} invoice(s) not yet matched")
    for c in m["discriminating_cases"]:
        verdict = "correct" if c["correct"] else "WRONG"
        print(f"  near-miss  : {c['invoice_id']} 1.5% qty variance -> "
              f"{c['predicted']} ({verdict}; the case that proves tolerance works)")

    print(f"\n  {'class':<20}{'prec':>7}{'rec':>7}{'F1':>7}{'supp':>7}")
    print("  " + "-" * 48)
    for cls in CLASSES:
        c = m["per_class"][cls]
        if c["support"] == 0 and c["fp"] == 0:
            print(f"  {cls:<20}{'--':>7}{'--':>7}{'--':>7}{0:>7}   (not present)")
        else:
            print(f"  {cls:<20}{c['precision']:>7}{c['recall']:>7}{c['f1']:>7}{c['support']:>7}")

    print("\n  confusion matrix (row = injected fault, column = system's verdict)")
    width = max(len(c) for c in CLASSES) + 2
    print("  " + " " * width + "".join(f"{c[:9]:>11}" for c in CLASSES))
    for expected in CLASSES:
        row = m["confusion_matrix"][expected]
        if sum(row.values()) == 0:
            continue
        print(f"  {expected:<{width}}" + "".join(f"{row[p]:>11}" for p in CLASSES))

    if m["misclassified"]:
        print("\n  MISCLASSIFIED:")
        for c in m["misclassified"]:
            print(f"    {c['invoice_id']} ({c['scenario']}): expected {c['expected']}, "
                  f"got {c['predicted']}")

    op = results["suites"]["operational"]
    print("\n" + "=" * 66)
    print("  OPERATIONAL KPIs")
    print("=" * 66)
    for key, value in op["kpis"].items():
        print(f"  {key:<38} {value}")
    print(f"  {'supplier_acceptance_rate':<38} {op['supplier_acceptance_rate']}")
    print(f"\n  exception mix ({op['exceptions_total']} total, "
          f"{op['exceptions_resolved']} resolved):")
    for b in op["exception_breakdown"]:
        print(f"    {b['exception_type']:<22}{b['severity']:<10}{b['count']}")

    for name in ("nlp_parse", "ocr"):
        suite = results["suites"][name]
        print("\n" + "=" * 66)
        print(f"  {name.upper().replace('_', ' ')}")
        print("=" * 66)
        if suite.get("status") != "measured":
            print(f"  {suite['status'].upper()}: {suite['reason']}")
            continue
        print(f"  provider        : {suite['provider']}")
        if not suite["score_is_purely_the_model"]:
            print(f"  ** WARNING: {suite['cases_via_fallback_stub']} case(s) fell back to the "
                  f"deterministic stub; this score is not purely the model **")
        else:
            print(f"  live model      : all {suite['cases_via_live_model']} case(s), no fallback")
        print(f"  field accuracy  : {suite['field_accuracy']}  "
              f"over {suite['fields_graded']} field(s)")
        print(f"  exact-match     : {suite['exact_match_rate']}  "
              f"({suite['exact_match_cases']}/{suite['cases']} cases fully correct)")
        print(f"\n  {'field':<22}{'acc':>7}{'n':>7}")
        print("  " + "-" * 36)
        for field, s in suite["per_field"].items():
            print(f"  {field:<22}{s['accuracy']:>7}{s['n']:>7}")
        if suite["failures"]:
            print("\n  FAILED CASES:")
            for f in suite["failures"]:
                if f["kind"] == "ambiguous":
                    print(f"    {f['id']}: did not flag -- {f['text']!r} "
                          f"-> {f['material_id']}, qty {f['qty']}")
                else:
                    wrong = {k: v for k, v in f["fields"].items() if not v["correct"]}
                    print(f"    {f['id']}: {f['text']!r}")
                    for field, v in wrong.items():
                        print(f"        {field}: expected {v['expected']!r}, "
                              f"got {v['actual']!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--nlp", action="store_true",
                    help="run the NLP parse suite (30 live API calls)")
    ap.add_argument("--ocr", action="store_true", help="run the OCR suite")
    ap.add_argument("--all", action="store_true", help="run every suite")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH,
                    help=f"where to write results (default {DEFAULT_OUT_PATH})")
    args = ap.parse_args()
    want_nlp = args.nlp or args.all
    want_ocr = args.ocr or args.all

    if not GROUND_TRUTH_PATH.exists():
        print(f"no ground truth at {GROUND_TRUTH_PATH}. Run backend/seed/seed.py first.",
              file=sys.stderr)
        return 2
    ground_truth = json.loads(GROUND_TRUTH_PATH.read_text())

    with get_conn() as conn:
        cur = conn.cursor()
        suites = {
            "match_classifier": run_match_classifier(cur, ground_truth["chains"]),
            "operational": run_operational(cur),
            "nlp_parse": (run_nlp(cur) if want_nlp else
                          {"status": "skipped",
                           "reason": "not requested; re-run with --nlp (costs live API calls)"}),
            "ocr": (run_ocr(cur) if want_ocr else
                    {"status": "skipped", "reason": "not requested; re-run with --ocr"}),
        }
        conn.rollback()

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ground_truth_seed": ground_truth.get("seed"),
        "ground_truth_generated_at": ground_truth.get("generated_at"),
        "chains_in_ground_truth": len(ground_truth["chains"]),
        "suites": suites,
        "measured_from": "this run's own database; no figure here is a vendor-supplied target",
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, default=str) + "\n")
    _print_report(results)
    print(f"\nwritten to {args.out}")
    print("served by GET /kpi/model-performance\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
