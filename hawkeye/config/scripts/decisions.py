#!/usr/bin/env python3
"""Decision inbox for items that need a human call.

Why a file and not the session todo list: a todo belongs to ONE session (that is a feature — a
list shared across projects is how you confuse an nginx decision with an Android one). Decisions
that arrive while nobody is in the chat have to survive the session that produced them, so the
durable record lives here and the session todo list is only the view a person sees when they are
back.

Usage:
  hawkeye-decisions.py add --title "..." --why "..." [--options "A|B"] [--default A]
                           [--origin "#12"] [--session <sid>]
  hawkeye-decisions.py list [--open-only] [--json]
  hawkeye-decisions.py answer <id> --answer "..." [--note "..."]
  hawkeye-decisions.py todo [--session <sid>]     # print todo items to mirror into a session

Storage: <inbox>/decisions.jsonl, append-only for adds, rewritten atomically for answers.
Nothing here calls a model: it is the zero-token half of the loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_INBOX = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "cache" / "hawkeye" / "inbox"


def inbox_path(explicit: str | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else DEFAULT_INBOX / "decisions.jsonl"


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _rewrite(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, path)


def _next_id(rows: list[dict]) -> str:
    used = {r.get("id") for r in rows}
    for n in range(1, 1000):
        cand = "d%d" % n
        if cand not in used:
            return cand
    raise SystemExit("decision ids exhausted")


def cmd_add(args, path: Path) -> int:
    rows = _load(path)
    rec = {
        "id": _next_id(rows),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "title": args.title,
        "why": args.why or "",
        "options": [o for o in (args.options or "").split("|") if o],
        "default": args.default or "",
        "origin": args.origin or "",
        "session": args.session or "",
        "status": "open",
        "answer": "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))
    return 0


def cmd_list(args, path: Path) -> int:
    rows = _load(path)
    if args.open_only:
        rows = [r for r in rows if r.get("status") == "open"]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no decisions)")
        return 0
    for r in rows:
        flag = "?" if r.get("status") == "open" else "✓"
        opts = ("  [" + " | ".join(r.get("options") or []) + "]") if r.get("options") else ""
        dflt = ("  default=%s" % r["default"]) if r.get("default") else ""
        print("%s %-4s %s%s%s" % (flag, r.get("id"), r.get("title"), opts, dflt))
        if r.get("why"):
            print("      why: %s" % r["why"])
        if r.get("status") != "open":
            print("      answered: %s" % r.get("answer", ""))
    return 0


def cmd_answer(args, path: Path) -> int:
    rows = _load(path)
    hit = next((r for r in rows if r.get("id") == args.id), None)
    if hit is None:
        sys.stderr.write("no such decision: %s\n" % args.id)
        return 1
    hit["status"] = "answered"
    hit["answer"] = args.answer + ((" — " + args.note) if args.note else "")
    hit["answered_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _rewrite(path, rows)
    print(json.dumps(hit, ensure_ascii=False))
    return 0


def cmd_todo(args, path: Path) -> int:
    """Print the open decisions as todo items: id, title, options and the default.

    The caller writes these into the session with the todo tool. Keeping the rendering here means
    the wording cannot drift between the file and what a person reads in the UI.
    """
    rows = [r for r in _load(path) if r.get("status") == "open"]
    if args.session:
        rows = [r for r in rows if (r.get("session") or "") in ("", args.session)]
    out = []
    for r in rows:
        bits = [r.get("title", "")]
        if r.get("options"):
            bits.append("options: " + " / ".join(r["options"]))
        if r.get("default"):
            bits.append("default: " + r["default"])
        out.append({"id": r["id"], "content": args.prefix + "; ".join(bits)})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="decision inbox: durable items that need a human call")
    p.add_argument("--file", help="decisions.jsonl path (default <home>/cache/hawkeye/inbox/decisions.jsonl)")
    p.add_argument("--prefix", default="NEEDS DECISION: ", help="prefix for rendered todo items")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--prefix", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    a = sub.add_parser("add", parents=[common])
    a.add_argument("--title", required=True)
    a.add_argument("--why", default="")
    a.add_argument("--options", default="", help="pipe-separated, e.g. 'A|B'")
    a.add_argument("--default", default="")
    a.add_argument("--origin", default="", help="issue/card/run it came from")
    a.add_argument("--session", default="", help="session id that should see it")

    l = sub.add_parser("list", parents=[common])
    l.add_argument("--open-only", action="store_true")
    l.add_argument("--json", action="store_true")

    an = sub.add_parser("answer", parents=[common])
    an.add_argument("id")
    an.add_argument("--answer", required=True)
    an.add_argument("--note", default="")

    t = sub.add_parser("todo", parents=[common])
    t.add_argument("--session", default="")

    args = p.parse_args()
    path = inbox_path(getattr(args, "file", None))
    return {"add": cmd_add, "list": cmd_list, "answer": cmd_answer, "todo": cmd_todo}[args.cmd](args, path)


if __name__ == "__main__":
    raise SystemExit(main())
