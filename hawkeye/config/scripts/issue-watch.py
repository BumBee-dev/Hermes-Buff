#!/usr/bin/env python3
"""Report new or changed issues/comments in a GitHub repository.

Written for the maintenance loop of this project: a scheduled agent turn runs it, and only
when something is actually new does it spend a turn on triage. The tool itself is dumb on
purpose — it fetches, diffs against a state file, and prints. All judgement stays outside.

State is one JSON file so the loop is resumable and idempotent:

    {"issues": {"3": {"updated_at": "...", "last_comment_id": 1234}}}

Exit codes, so a wrapper can branch without parsing stdout:

    0   nothing new
    10  new or changed items (printed)
    2   error (no token, HTTP failure, bad JSON)

Usage:

    GITHUB_TOKEN=... issue-watch.py --repo eSun-ws/Hermes-Buff --state /path/state.json
    ... --mark-seen        # only after the items have been handled
    ... --json             # machine-readable output instead of a summary

Read-only by default: the only write is ``--mark-seen`` touching the local state file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def _token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _get(url: str, token: str) -> object:
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hawkeye-issue-watch",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"issues": {}}
    if not isinstance(data.get("issues"), dict):
        data["issues"] = {}
    return data


def _is_news(issue: dict, seen: dict, new_comments: list[dict]) -> bool:
    """Does this issue warrant a wake?

    Open issues: any movement counts (a new issue, an edited body, a new comment). Closed issues:
    only a *reply* counts. Closing is the maintainer's own act, and a closed issue that was never
    seen before is history, not news -- reporting it would dump every old comment the first time the
    state file is missing.

    This distinction is the fix for a real miss: the watcher used to query ``state=open``, so a
    reporter's follow-up on a closed issue was invisible. On a repo where every issue ends up
    closed, that is most of the conversation.
    """
    if not new_comments:
        return issue.get("state") == "open" and not seen
    return True


def collect(repo: str, token: str, state: dict, ignore_authors: set[str] | None = None,
            track: bool = False) -> list[dict]:
    """Issues with movement since the last look, open ones and replies on closed ones.

    ``ignore_authors`` suppresses items whose only new activity is by those logins — the
    maintainer's own replies bump ``updated_at`` on GitHub and would otherwise wake the
    watcher for its own comments.

    ``track`` records a marker for every issue examined (not only the reported ones), which is what
    makes "closed issue, never seen" safe to treat as history: the marker exists from the first
    pass, so a later reply is genuinely new.
    """
    ignore = {a.lower() for a in (ignore_authors or set())}
    items: list[dict] = []
    page = 1
    while True:
        # state=all, newest first: a closed issue is where a reporter's follow-up lands, and paging
        # oldest-first over a long history would burn the rate limit before reaching it.
        batch = _get("%s/repos/%s/issues?state=all&sort=updated&direction=desc&per_page=50&page=%d"
                     % (API, repo, page), token)
        if not isinstance(batch, list) or not batch:
            break
        for issue in batch:
            if issue.get("pull_request"):
                continue                       # PRs have their own review flow
            number = str(issue.get("number"))
            seen = state.get("issues", {}).get(number) or {}
            if seen and seen.get("updated_at") == issue.get("updated_at") \
                    and not seen.get("last_comment_id"):
                continue                       # untouched and provably comment-free
            if seen and seen.get("updated_at") == issue.get("updated_at"):
                # Nothing moved at all: no comment to fetch. One request saved per quiet issue per
                # poll, which on a 14-issue repo is the difference between 15 requests and 1+.
                continue
            comments = _get("%s/repos/%s/issues/%s/comments?per_page=100" % (API, repo, number), token)
            last_id = comments[-1]["id"] if isinstance(comments, list) and comments else 0
            new_comments = [c for c in (comments if isinstance(comments, list) else [])
                            if c.get("id", 0) > (seen.get("last_comment_id") or 0)]
            if track:
                state.setdefault("issues", {})[number] = {
                    "updated_at": issue.get("updated_at"),
                    "last_comment_id": last_id,
                }
            if new_comments and all(
                    str((c.get("user") or {}).get("login") or "").lower() in ignore
                    for c in new_comments):
                continue                       # only our own replies: not news
            if not _is_news(issue, seen, new_comments):
                continue
            items.append({
                "number": issue.get("number"),
                "title": issue.get("title"),
                "url": issue.get("html_url"),
                "author": (issue.get("user") or {}).get("login"),
                "created_at": issue.get("created_at"),
                "updated_at": issue.get("updated_at"),
                "body": issue.get("body") or "",
                "new_comments": new_comments,
                "first_seen": not seen,
                "updated_at_marker": issue.get("updated_at"),
                "last_comment_id": last_id,
            })
        if len(batch) < 50:
            break
        page += 1
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Report new or changed GitHub issues.")
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--state", default="", help="state file (default: none, everything looks new)")
    parser.add_argument("--mark-seen", action="store_true", help="write the current state back")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--max-body", type=int, default=4000)
    parser.add_argument("--ignore-authors", default="",
                        help="comma-separated logins whose comments are not news (e.g. the maintainer)")
    args = parser.parse_args()

    token = _token()
    if not token:
        print("issue-watch: no GITHUB_TOKEN/GH_TOKEN in the environment", file=sys.stderr)
        return 2
    state = _state(args.state) if args.state else {"issues": {}}
    try:
        items = collect(args.repo, token, state,
                        {a.strip() for a in args.ignore_authors.split(",") if a.strip()},
                        track=args.mark_seen)
    except urllib.error.HTTPError as exc:
        print("issue-watch: HTTP %s from the API (%s)" % (exc.code, exc.reason), file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print("issue-watch: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        if not items:
            print("nothing new on %s" % args.repo)
        for item in items:
            print("=" * 72)
            print("#%s %s" % (item["number"], item["title"]))
            print("  by %s | updated %s | %s" % (item["author"], item["updated_at"], item["url"]))
            print("  body:")
            body = item["body"]
            print(body[: args.max_body] + ("\n  …(truncated)" if len(body) > args.max_body else ""))
            for comment in item["new_comments"]:
                print("  --- comment by %s @ %s ---"
                      % ((comment.get("user") or {}).get("login"), comment.get("created_at")))
                text = comment.get("body") or ""
                print(text[: args.max_body] + ("\n  …(truncated)" if len(text) > args.max_body else ""))

    if args.mark_seen and args.state:
        # collect(track=True) already wrote a marker for every issue it looked at; writing only the
        # reported ones is what made "closed issue, never seen" indistinguishable from history.
        os.makedirs(os.path.dirname(os.path.abspath(args.state)) or ".", exist_ok=True)
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, args.state)

    return 10 if items else 0


if __name__ == "__main__":
    raise SystemExit(main())
