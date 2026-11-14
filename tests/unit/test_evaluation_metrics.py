"""ARCHITECTURE §12.3 metrics with hand-computed expected results (approved decision D-G4).
Pure: datagen.evaluation.compute on plain findings and labels, no database."""

from datagen.evaluation import F, L, compute

STUDENTS = [f"STU-{n:06d}" for n in range(1, 11)]


def lab(i, t, rec, stu=1, related=()):
    return L(f"GT-2026-{i:06d}", t, rec, f"STU-{stu:06d}", tuple(related))


def fnd(i, t, rec, stu=1):
    return F(f"FND-2026-{i:06d}", t, rec, f"STU-{stu:06d}")


def run(findings, labels, not_reconciled=()):
    return compute(findings, labels, students=STUDENTS, not_reconciled=not_reconciled)


def test_perfect_detection():
    labels = [lab(1, "DUPLICATE_INVOICE", "INV-2"), lab(2, "BALANCE_LEDGER_MISMATCH", "SNAP-1", 2)]
    r = run(
        [fnd(1, "DUPLICATE_INVOICE", "INV-2"), fnd(2, "BALANCE_LEDGER_MISMATCH", "SNAP-1", 2)],
        labels,
    )
    m = r["by_type"]["DUPLICATE_INVOICE"]
    assert (m["tp"], m["fp"], m["fn"], m["precision"], m["recall"], m["f1"]) == (
        1,
        0,
        0,
        "1.0000",
        "1.0000",
        "1.0000",
    )
    assert r["meets_targets"] is True
    assert r["students"]["non_anomalous"] == 8
    assert r["students"]["pct_non_anomalous_reconciled"] == "100.00"


def test_hand_computed_two_thirds():
    """TP 2, FP 1, FN 1 → P = R = F1 = 2/3 = 0.6667."""
    t = "PAYMENT_OVER_ALLOCATED"
    labels = [lab(1, t, "PAY-1"), lab(2, t, "PAY-2", 2), lab(3, t, "PAY-3", 3)]
    findings = [fnd(1, t, "PAY-1"), fnd(2, t, "PAY-2", 2), fnd(3, t, "PAY-9", 9)]
    m = run(findings, labels)["by_type"][t]
    assert (m["tp"], m["fp"], m["fn"]) == (2, 1, 1)
    assert (m["precision"], m["recall"], m["f1"]) == ("0.6667", "0.6667", "0.6667")


def test_hand_computed_unequal_precision_and_recall():
    """TP 3, FP 1, FN 2 → P = 0.75, R = 0.6, F1 = 2·0.75·0.6/1.35 = 0.6667."""
    t = "REFUND_EXCEEDS_NET_PAID"
    labels = [lab(i, t, f"REF-{i}", i) for i in range(1, 6)]
    findings = [fnd(i, t, f"REF-{i}", i) for i in range(1, 4)] + [fnd(9, t, "REF-9", 9)]
    m = run(findings, labels)["by_type"][t]
    assert (m["tp"], m["fp"], m["fn"]) == (3, 1, 2)
    assert (m["precision"], m["recall"], m["f1"]) == ("0.7500", "0.6000", "0.6667")
    r = run(findings, labels)
    assert r["overall"]["precision"] == "0.7500" and r["meets_targets"] is False


def test_consequential_finding_of_another_type_is_not_a_false_positive():
    """The P6-O3 case: a refund label lists its payment; the payment's PAYMENT_OVER_ALLOCATED
    finding is consequential (another type than the label), not an FP."""
    labels = [lab(1, "REFUND_EXCEEDS_NET_PAID", "REF-1", related=["PAY-7", "LED-1"])]
    findings = [
        fnd(1, "REFUND_EXCEEDS_NET_PAID", "REF-1"),
        fnd(2, "PAYMENT_OVER_ALLOCATED", "PAY-7"),
    ]
    r = run(findings, labels)
    assert r["consequential"] == 1 and r["unexpected"] == 0
    assert r["by_type"]["PAYMENT_OVER_ALLOCATED"]["fp"] == 0
    assert r["by_type"]["PAYMENT_OVER_ALLOCATED"]["consequential"] == 1
    assert r["consequential_findings"][0]["caused_by_labels"] == ["GT-2026-000001"]
    assert r["meets_targets"] is True


def test_same_type_on_a_related_record_is_a_false_positive():
    """Consequential needs another type than the label that lists the record."""
    t = "DUPLICATE_INVOICE"
    labels = [lab(1, t, "INV-2", related=["INV-1"])]
    r = run([fnd(1, t, "INV-2"), fnd(2, t, "INV-1")], labels)
    assert r["by_type"][t]["fp"] == 1 and r["consequential"] == 0 and r["unexpected"] == 1


def test_a_finding_on_clean_data_is_unexpected_and_other_types_are_not_fps():
    labels = [lab(1, "DUPLICATE_INVOICE", "INV-2")]
    findings = [fnd(1, "DUPLICATE_INVOICE", "INV-2"), fnd(2, "DATE_ORDER", "LED-5", 4)]
    r = run(findings, labels)
    assert r["unexpected"] == 1 and r["unexpected_by_type"] == {"DATE_ORDER": 1}
    assert all(m["fp"] == 0 for m in r["by_type"].values())
    assert r["meets_targets"] is False


def test_a_repeated_finding_counts_once_as_tp_then_as_fp():
    t = "BALANCE_LEDGER_MISMATCH"
    r = run([fnd(1, t, "SNAP-1"), fnd(2, t, "SNAP-1")], [lab(1, t, "SNAP-1")])
    assert (r["by_type"][t]["tp"], r["by_type"][t]["fp"]) == (1, 1)


def test_zero_over_zero_is_null_and_missed_everything_is_zero():
    r = run([], [lab(1, "DUPLICATE_INVOICE", "INV-2")])
    dup = r["by_type"]["DUPLICATE_INVOICE"]
    assert (dup["precision"], dup["recall"], dup["f1"]) == (None, "0.0000", None)
    sch = r["by_type"]["SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"]
    assert (sch["precision"], sch["recall"], sch["f1"]) == (None, None, None)
    t = "PAYMENT_OVER_ALLOCATED"  # only wrong findings: P = 0, R = 0, F1 = 0
    r = run([fnd(1, t, "PAY-9")], [lab(1, t, "PAY-1")])
    assert (r["by_type"][t]["precision"], r["by_type"][t]["recall"], r["by_type"][t]["f1"]) == (
        "0.0000",
        "0.0000",
        "0.0000",
    )
    assert r["false_negatives"] == ["GT-2026-000001"]


def test_non_anomalous_students_must_all_reconcile():
    labels = [lab(1, "DUPLICATE_INVOICE", "INV-2", stu=1)]
    r = run(
        [fnd(1, "DUPLICATE_INVOICE", "INV-2")], labels, not_reconciled=["STU-000001", "STU-000005"]
    )
    s = r["students"]
    assert s["non_anomalous"] == 9  # STU-000001 carries a label: not counted
    assert s["non_anomalous_not_reconciled"] == ["STU-000005"]
    assert s["pct_non_anomalous_reconciled"] == "88.89"  # 8 / 9
    assert r["meets_targets"] is False
