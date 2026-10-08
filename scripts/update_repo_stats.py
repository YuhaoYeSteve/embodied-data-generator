#!/usr/bin/env python3
"""Collect README badges and history data from authenticated GitHub metadata.

Run with gh authenticated locally, or GH_TOKEN set by GitHub Actions. Credentials
are never passed in command arguments or written to the generated assets.
"""

import argparse
import json
import re
import subprocess
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path


QUERY = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    stargazerCount
    forkCount
    watchers { totalCount }
    issues(states: OPEN) { totalCount }
    pullRequests(states: OPEN) { totalCount }
  }
}"""
BADGES = (
    ("stars", "Stars", "#55d6be"),
    ("forks", "Forks", "#91b7ff"),
    ("watchers", "Watchers", "#c1a0f7"),
    ("issues", "Open issues", "#f6c676"),
    ("pull_requests", "Open PRs", "#55d6be"),
)
CI_LABELS = {
    "success": "passed", "failure": "failed", "timed_out": "failed",
    "startup_failure": "failed", "action_required": "failed",
    "cancelled": "cancelled", "skipped": "skipped", "neutral": "neutral",
    None: "unknown",
}


def validate_repo(repository):
    pattern = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}"
    if not re.fullmatch(pattern, repository) or repository.split("/")[-1] in (".", ".."):
        raise ValueError("Repository must be a GitHub owner/name identifier.")
    return repository


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("GitHub statistics must be nonnegative integers.")
    return value


def parse_snapshot(repository, payload, sampled_at):
    validate_repo(repository)
    try:
        datetime.strptime(sampled_at, "%Y-%m-%dT%H:%M:%SZ")
        if payload.get("errors"):
            raise ValueError("GitHub did not return complete repository metadata.")
        result = payload["data"]["repository"]
        if result["nameWithOwner"].lower() != repository.lower():
            raise ValueError("GitHub returned a different repository.")
        # Only these metadata fields are persisted, never raw responses or env.
        return {
            "repository": repository,
            "sampled_at": sampled_at,
            "source": "https://api.github.com/graphql",
            "stars": count(result["stargazerCount"]),
            "forks": count(result["forkCount"]),
            "watchers": count(result["watchers"]["totalCount"]),
            "issues": count(result["issues"]["totalCount"]),
            "pull_requests": count(result["pullRequests"]["totalCount"]),
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("GitHub did not return complete repository metadata.") from exc


def fetch_snapshot(repository, sampled_at=None):
    validate_repo(repository)
    owner, name = repository.split("/")
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", "query=" + QUERY,
             "-F", "owner=" + owner, "-F", "name=" + name],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        # Do not echo arbitrary CLI diagnostics into logs or generated content.
        raise RuntimeError("GitHub request failed; check gh authentication and repository access.") from None
    if sampled_at is None:
        sampled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = parse_snapshot(repository, json.loads(result.stdout), sampled_at)
    snapshot["ci"] = fetch_ci(repository)
    return snapshot


def parse_ci(repository, payload):
    validate_repo(repository)
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("GitHub did not return valid CI metadata.")
    for run in payload["workflow_runs"]:
        # Ignore PR and fork runs even when their branch is also named main.
        if (run.get("head_branch") != "main"
                or run.get("head_repository", {}).get("full_name", "").lower() != repository.lower()
                or run.get("event") in ("pull_request", "pull_request_target")):
            continue
        status, conclusion = run["status"], run["conclusion"]
        if status not in ("queued", "in_progress", "waiting", "requested", "pending", "completed"):
            raise ValueError("GitHub returned an unknown CI state.")
        if conclusion not in CI_LABELS:
            raise ValueError("GitHub returned an unknown CI conclusion.")
        if (not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
                or not re.fullmatch("https://github.com/" + re.escape(repository) + r"/actions/runs/[0-9]+", run["html_url"], re.I)):
            raise ValueError("GitHub returned an invalid CI run reference.")
        return {
            "label": CI_LABELS[conclusion] if status == "completed" else "pending",
            "status": status, "conclusion": conclusion,
            "run_url": run["html_url"], "head_sha": run["head_sha"],
        }
    return {"label": "not run", "status": "not_run", "conclusion": None,
            "run_url": None, "head_sha": None}


def fetch_ci(repository):
    validate_repo(repository)
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/workflows/ci.yml/runs?branch=main&per_page=20"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        if re.search(r"\bHTTP 404\b", exc.stderr or ""):
            return parse_ci(repository, {"workflow_runs": []})
        raise RuntimeError("GitHub CI request failed; previous assets were preserved.") from None
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError("GitHub CI request failed; previous assets were preserved.") from None
    return parse_ci(repository, json.loads(result.stdout))


def update_history(previous, snapshot):
    repository = validate_repo(snapshot["repository"])
    today = snapshot["sampled_at"][:10]
    date.fromisoformat(today)
    samples = {}
    if previous is not None:
        if previous.get("repository") != repository:
            raise ValueError("History belongs to a different repository.")
        for sample in previous["samples"]:
            day = sample["date"]
            if date.fromisoformat(day).isoformat() != day or day > today:
                raise ValueError("History cannot contain future or invalid dates.")
            samples[day] = count(sample["stars"])
    # Re-running today refreshes its observation. Decreasing totals are valid.
    samples[today] = count(snapshot["stars"])
    return {
        "repository": repository,
        "sampling": "Daily UTC snapshots; no data before the first observation.",
        "samples": [{"date": day, "stars": stars} for day, stars in sorted(samples.items())],
    }


def render_badge(label, value, color, sampled_at):
    value = str(value)
    left = max(50, 18 + len(label) * 7)
    right = max(34, 18 + len(value) * 8)
    width = left + right
    title = escape(f"{label}: {value}; sampled {sampled_at}")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="28" role="img" aria-label="{title}">
  <title>{title}</title>
  <clipPath id="r"><rect width="{width}" height="28" rx="6"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{width}" height="28" fill="#202939"/>
    <rect x="{left}" width="{right}" height="28" fill="{color}"/>
  </g>
  <g text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif" font-size="12">
    <text x="{left / 2}" y="18" fill="#edf2f7">{escape(label)}</text>
    <text x="{left + right / 2}" y="18" fill="#13232b" font-weight="700">{escape(value)}</text>
  </g>
</svg>
'''


def write_assets(output, snapshot):
    history_path = output / "history.json"
    previous = json.loads(history_path.read_text()) if history_path.exists() else None
    history = update_history(previous, snapshot)
    assets = {
        "stats.json": json.dumps(snapshot, indent=2) + "\n",
        "history.json": json.dumps(history, indent=2) + "\n",
    }
    for key, label, color in BADGES:
        filename = "pull-requests.svg" if key == "pull_requests" else key + ".svg"
        assets[filename] = render_badge(label, snapshot[key], color, snapshot["sampled_at"])
    ci_label = snapshot.get("ci", {}).get("label", "not sampled")
    ci_color = {"passed": "#55d6be", "failed": "#f59c99", "pending": "#f6c676"}.get(ci_label, "#b6c0d0")
    assets["ci.svg"] = render_badge("CI", ci_label, ci_color, snapshot["sampled_at"])
    output.mkdir(parents=True, exist_ok=True)
    for filename, content in assets.items():
        target = output / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/name (no URL)")
    parser.add_argument("--output", type=Path, default=Path("docs/assets/community"))
    args = parser.parse_args()
    try:
        snapshot = fetch_snapshot(args.repo)
        write_assets(args.output, snapshot)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        parser.exit(1, "Unable to update repository statistics; check gh access and the history file.\n")
    print(f'Updated repository statistics for {snapshot["repository"]} at {snapshot["sampled_at"]}.')


if __name__ == "__main__":
    main()
