#!/usr/bin/env python3
"""Read-only GitHub PR-head check observation.

One GraphQL query reads the pull request's head commit and its
statusCheckRollup per-state counts, so the head and the counts come from the
same moment. These are the checks GitHub shows on the pull request; manually
dispatched workflow runs (workflow_dispatch) on the same commit are not
included. No API error text is included in output.
"""

import argparse
import json
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-fA-F]{7,40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TIMEOUT_SECONDS = 30
QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts {
        checkRunCountsByState { state count }
        statusContextCountsByState { state count }
      } } } } }
    }
  }
}
"""
# CheckRunState and StatusState values that mean a check is done. Any other
# state, including one GitHub adds later, counts as unfinished so the wait
# leans toward pending rather than met. Cancelled, skipped, neutral and stale
# count as finished but not failed, as in app/github_checks.py.
FINISHED = {"SUCCESS", "COMPLETED", "CANCELLED", "SKIPPED", "NEUTRAL", "STALE",
            "FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}


class CheckError(Exception):
    """An observation cannot be trusted; the message is safe for display."""


def query(repo, pr):
    owner, name = repo.split("/")
    try:
        process = subprocess.run(
            ["gh", "api", "graphql", "-f", "query=" + QUERY, "-f", "owner=" + owner,
             "-f", "name=" + name, "-F", f"number={pr}"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError("Could not read GitHub checks; try again later.") from exc
    if process.returncode:
        raise CheckError("Could not read GitHub checks; verify access and try again later.")
    try:
        commit = json.loads(process.stdout)["data"]["repository"]["pullRequest"]["commits"]["nodes"][-1]["commit"]
        head = commit["oid"]
        contexts = (commit["statusCheckRollup"] or {}).get("contexts") or {}
        counts = {}
        for key in ("checkRunCountsByState", "statusContextCountsByState"):
            for item in contexts.get(key) or []:
                if type(item["count"]) is not int or item["count"] < 0:
                    raise ValueError(item)
                counts[item["state"]] = counts.get(item["state"], 0) + item["count"]
    except (ValueError, LookupError, TypeError, AttributeError) as exc:
        raise CheckError("GitHub returned an unreadable check response; try again later.") from exc
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise CheckError("GitHub did not provide a valid PR head; try again later.")
    return head, counts


def observe(repo, pr, expected_sha):
    head, counts = query(repo, pr)
    if not head.lower().startswith(expected_sha.lower()):
        return result("failed", "The requested commit is not the published pull-request head.", 0, 0)
    total = sum(counts.values())
    completed = sum(n for state, n in counts.items() if state in FINISHED)
    failed = sum(n for state, n in counts.items() if state in FAILED)
    if total == 0:
        return result("pending", "No checks have appeared for this commit yet.", 0, 0)
    if completed < total:
        return result("pending", f"{completed} of {total} checks have finished; waiting for the rest.", completed, total)
    if failed:
        return result("met", f"All {total} checks finished; {failed} reported failure.", completed, total)
    return result("met", f"All {total} checks finished.", completed, total)


def result(state, summary, completed, total):
    return {"state": state, "summary": summary, "completed": completed, "total": total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print one bounded JSON result")
    parser.add_argument("repo")
    parser.add_argument("pr")
    parser.add_argument("sha")
    args = parser.parse_args()
    if not REPO.fullmatch(args.repo) or not args.pr.isdecimal() or int(args.pr) <= 0 or not SHA.fullmatch(args.sha):
        parser.error("expected owner/repo, positive PR number, and 7-40 hexadecimal SHA characters")
    try:
        value = observe(args.repo, args.pr, args.sha)
    except CheckError as exc:
        value = result("failed", str(exc), 0, 0)
    if args.json:
        print(json.dumps(value, separators=(",", ":")))
        return 0
    if value["state"] == "pending":
        return 1
    if value["state"] == "failed":
        print("pr-checks: " + value["summary"], file=sys.stderr)
        return 2
    print(value["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
