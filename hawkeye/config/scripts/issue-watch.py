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


def collect(repo: str, token: str, state: dict) -> list[dict]:
    """Every open issue whose updated_at or last comment moved since the last look."""
    items: list[dict] = []
    page = 1
    while True:
        batch = _get("%s/repos/%s/issues?state=open&per_page=50&page=%d" % (API, repo, page), token)
        if not isinstance(batch, list) or not batch:
            break
        for issue in batch:
            if issue.get("pull_request"):
                continue                       # PRs have their own review flow
            number = str(issue.get("number"))
            comments = _get("%s/repos/%s/issues/%s/comments?per_page=100" % (API, repo, number), token)
            last_id = comments[-1]["id"] if isinstance(comments, list) and comments else 0
            seen = state.get("issues", {}).get(number) or {}
            changed = (seen.get("updated_at") != issue.get("updated_at")
                       or seen.get("last_comment_id") != last_id)
            if changed:
                items.append({
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "url": issue.get("html_url"),
                    "author": (issue.get("user") or {}).get("login"),
                    "created_at": issue.get("created_at"),
                    "updated_at": issue.get("updated_at"),
                    "body": issue.get("body") or "",
                    "new_comments": [c for c in (comments if isinstance(comments, list) else [])
                                     if c.get("id", 0) > (seen.get("last_comment_id") or 0)],
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
    args = parser.parse_args()

    token = _token()
    if not token:
        print("issue-watch: no GITHUB_TOKEN/GH_TOKEN in the environment", file=sys.stderr)
        return 2
    state = _state(args.state) if args.state else {"issues": {}}
    try:
        items = collect(args.repo, token, state)
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
        for item in items:
            state.setdefault("issues", {})[str(item["number"])] = {
                "updated_at": item["updated_at_marker"],
                "last_comment_id": item["last_comment_id"],
            }
        os.makedirs(os.path.dirname(os.path.abspath(args.state)) or ".", exist_ok=True)
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, args.state)

    return 10 if items else 0


if __name__ == "__main__":
    raise SystemExit(main())
