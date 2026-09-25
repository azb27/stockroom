"""Human approval of PO drafts. Not a tool: the agent cannot import or call this through the registry.

python -m stockroom.approvals list [--status PENDING_APPROVAL]
python -m stockroom.approvals show <draft_id>
python -m stockroom.approvals approve <draft_id> --by "Aziz" [--note "..."]
python -m stockroom.approvals reject  <draft_id> --by "Aziz" --note "reason"
"""

from __future__ import annotations

import argparse

import pandas as pd

from stockroom.appdb import app_session

DECISIONS = {"approve": "APPROVED", "reject": "REJECTED"}


class ApprovalError(ValueError):
    pass


def list_drafts(status: str | None = None) -> pd.DataFrame:
    q = "SELECT draft_id, created_at, store, supplier_id, status, n_lines, total_cases, est_cost FROM po_drafts"
    params: list = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    with app_session() as con:
        return con.execute(q + " ORDER BY created_at DESC", params).df()


def decide(draft_id: str, decision: str, by: str, note: str | None = None) -> None:
    if decision not in DECISIONS:
        raise ApprovalError(f"decision must be one of {list(DECISIONS)}")
    if not by or not by.strip():
        raise ApprovalError("a named person must make the decision (--by)")
    if decision == "reject" and not note:
        raise ApprovalError("a rejection needs a reason (--note)")
    with app_session() as cur:
        row = cur.execute("SELECT status FROM po_drafts WHERE draft_id = ?", [draft_id]).fetchone()
        if row is None:
            raise ApprovalError(f"no draft {draft_id}")
        if row[0] != "PENDING_APPROVAL":
            raise ApprovalError(f"draft {draft_id} is already {row[0]}")
        cur.execute(
            """UPDATE po_drafts SET status = ?, decided_at = now(), decided_by = ?, decision_note = ?
               WHERE draft_id = ? AND status = 'PENDING_APPROVAL'""",
            [DECISIONS[decision], by.strip(), note, draft_id],
        )


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m stockroom.approvals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    sh = sub.add_parser("show")
    sh.add_argument("draft_id")
    for name in DECISIONS:
        p = sub.add_parser(name)
        p.add_argument("draft_id")
        p.add_argument("--by", required=True)
        p.add_argument("--note")
    a = ap.parse_args()
    pd.set_option("display.width", 200)
    if a.cmd == "list":
        print(list_drafts(a.status).to_string(index=False))
    elif a.cmd == "show":
        with app_session() as cur:
            head = cur.execute("SELECT * FROM po_drafts WHERE draft_id = ?", [a.draft_id]).df()
            lines = cur.execute("SELECT * FROM po_draft_lines WHERE draft_id = ?", [a.draft_id]).df()
        print(head.T.to_string())
        print(lines.to_string(index=False))
    else:
        decide(a.draft_id, a.cmd, a.by, a.note)
        print(f"{a.draft_id}: {DECISIONS[a.cmd]} by {a.by}")


if __name__ == "__main__":
    main()
