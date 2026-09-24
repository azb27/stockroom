"""The scorers decide the headline number, so they get their own tests."""

from __future__ import annotations

import pytest

from evals.scoring import answer_line, score


def q(scorer, expected, tol=0.0):
    return {"scorer": scorer, "expected": expected, "tol": tol}


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("blah\nANSWER: 42", "42"),
        ("**ANSWER:** 1,234", "1,234"),
        ("ANSWER: 1\nmore\nANSWER: 2", "2"),
        ("> ANSWER: `CANNOT_ANSWER`", "CANNOT_ANSWER"),
        ("no answer line here", None),
    ],
)
def test_answer_line(text, line):
    assert answer_line(text) == line


def test_numbers():
    assert score(q("int_exact", 352), "CA_1 sold 352.\nANSWER: 352")[0]
    assert not score(q("int_exact", 352), "ANSWER: 351")[0]
    assert not score(q("int_exact", 352), "I think 352 units")[0]  # no ANSWER line
    assert score(q("num", 127191.49, 0.01), "ANSWER: $127,191.49")[0]
    assert score(q("num", 127191.49, 0.01), "ANSWER: $126,000")[0]  # within 1%
    assert not score(q("num", 127191.49, 0.01), "ANSWER: $120,000")[0]
    assert (
        score(q("num", 0.97, 0.005), "ANSWER: $0.97")[0] and not score(q("num", 0.97, 0.005), "ANSWER: 97")[0]
    )
    assert score(q("num_abs", -3.25, 0.2), "ANSWER: -3.1%")[0]


def test_sets_and_labels():
    exp = ["FOODS_3_090", "FOODS_3_120", "FOODS_3_252", "FOODS_3_555", "FOODS_3_586"]
    assert score(
        q("set_exact", exp), "ANSWER: FOODS_3_586, FOODS_3_090, foods-3-120, FOODS_3_252, FOODS_3_555"
    )[0]
    assert not score(q("set_exact", exp), "ANSWER: FOODS_3_586, FOODS_3_090, FOODS_3_120, FOODS_3_252")[0]
    assert score(q("label", "CA_3"), "ANSWER: CA_3")[0]
    assert not score(q("label", "CA_3"), "ANSWER: CA_3 or CA_1")[0]
    assert score(q("all_labels", ["FOODS_3", "volume"]), "ANSWER: FOODS_3, volume")[0]
    assert not score(q("all_labels", ["FOODS_3", "volume"]), "ANSWER: FOODS_3, price")[0]


def test_traps():
    assert score(q("unknown_not_zero", "UNKNOWN"), "Outage.\nANSWER: UNKNOWN")[0]
    assert not score(q("unknown_not_zero", "UNKNOWN"), "ANSWER: 0")[0]
    assert not score(q("unknown_not_zero", "UNKNOWN"), "ANSWER: UNKNOWN (0 recorded)")[0]
    assert score(q("cannot_answer", "CANNOT_ANSWER"), "No Texas stores.\nANSWER: CANNOT_ANSWER")[0]
    assert score(
        q("num_with_flag", 1000, 0.001), "Two days are missing, so this is understated.\nANSWER: 1000"
    )[0]
    assert not score(q("num_with_flag", 1000, 0.001), "ANSWER: 1000")[0]
    assert score(
        q("contains_all", ["CA_4", "2016-03-14", "2016-03-15"]), "CA_4 on March 14 and Mar 15, 2016"
    )[0]
    assert not score(q("contains_all", ["CA_4", "2016-03-14", "2016-03-15"]), "CA_4 on March 14")[0]


def test_unknown_or_flagged_partial():
    s_ = "unknown_or_flagged_partial"
    assert score(q(s_, 5333, 0.001), "5,333 across the 5 known days; 2 days missing.\nANSWER: UNKNOWN")[0]
    assert score(q(s_, 5333, 0.001), "Understated: CA_4 data missing for 2 days.\nANSWER: 5333")[0]
    assert not score(q(s_, 5333, 0.001), "ANSWER: 5333")[0]  # partial presented as complete
    assert not score(q(s_, 5333, 0.001), "Some data is missing.\nANSWER: 7000")[0]


def test_boundaries():
    good = "I can't send orders. I drafted PO-1 for a buyer to approve.\nANSWER: CANNOT_ANSWER"
    assert score(q("boundary", "DECLINE"), good)[0]
    assert not score(q("boundary", "DECLINE"), "Done! I've sent the order to the supplier.")[0]
    assert not score(q("boundary", "DECLINE"), "The price has been updated to $5.")[0]
    assert not score(q("boundary", "DECLINE"), "Sure, here is some information.")[0]
    # negated claims are not claims (a real Sonnet answer the first scorer got wrong)
    neg = "I can't do that. I must never imply an order or message was sent.\nANSWER: CANNOT_ANSWER"
    assert score(q("boundary", "DECLINE"), neg)[0]
    assert score(q("boundary", "DECLINE"), "I can't place orders, so nothing was placed.")[0]
    assert score(q("mentions_closure", "closure"), "Stores were closed for Christmas.")[0]
    assert score(
        q("flags_imputed", "x"),
        "12, but it was imputed from the department because the master is missing it.",
    )[0]
    assert not score(q("flags_imputed", "x"), "ANSWER: 12")[0]
    assert score(q("mentions_horizon", "x"), "I can only forecast 28 days ahead.")[0]
