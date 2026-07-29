"""Human approval CLI for analyst proposals (docs/ROADMAP.md task 7.3, I3).

This is the ONLY path that turns an analyst proposal into a live config version.
Approve applies the pending patch to the base config params and calls the
versioned loader (get_or_create_config) -> a NEW immutable version. Reject archives
the proposal with a reason. Nothing here is automated; a human runs it.

    python scripts/approve.py list
    python scripts/approve.py show   <id>
    python scripts/approve.py approve <id> [--notes "..."]
    python scripts/approve.py reject  <id> --reason "..."
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.approval import ApprovalError, approve_change, reject_change
from pipeline.common.db import make_engine
from pipeline.common.models import PendingChange


def _get(session: Session, change_id: str) -> PendingChange:
    change = session.get(PendingChange, change_id)
    if change is None:
        raise SystemExit(f"no pending_change with id {change_id!r}")
    return change


def cmd_list(session: Session) -> None:
    rows = (
        session.execute(select(PendingChange).order_by(PendingChange.created_at.desc()))
        .scalars()
        .all()
    )
    if not rows:
        print("(no proposals)")
        return
    for c in rows:
        print(f"{c.id}  {c.status:8}  base={c.base_config_version}  patch={c.patch_json}")


def cmd_show(session: Session, change_id: str) -> None:
    c = _get(session, change_id)
    print(f"id:      {c.id}\nstatus:  {c.status}\nbase:    {c.base_config_version}")
    print(f"patch:   {c.patch_json}\nrationale: {c.rationale}\n")
    print(c.report_md or "(no report)")


def cmd_approve(session: Session, change_id: str, notes: str) -> None:
    # Delegates to the shared human-gated path the API also uses (pipeline.common.approval).
    try:
        cfg = approve_change(session, change_id, notes=notes)
    except ApprovalError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"approved -> new config_version {cfg.config_version}")


def cmd_reject(session: Session, change_id: str, reason: str) -> None:
    try:
        reject_change(session, change_id, reason=reason)
    except ApprovalError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"rejected {change_id}: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("id")
    p_approve.add_argument("--notes", default="")
    p_reject = sub.add_parser("reject")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", required=True)
    args = parser.parse_args()

    engine = make_engine(args.url)
    with Session(engine) as session:
        if args.cmd == "list":
            cmd_list(session)
        elif args.cmd == "show":
            cmd_show(session, args.id)
        elif args.cmd == "approve":
            cmd_approve(session, args.id, args.notes)
        elif args.cmd == "reject":
            cmd_reject(session, args.id, args.reason)


if __name__ == "__main__":
    main()
