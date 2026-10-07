"""Offline checks for README statistics: real counts, no invented history or secrets."""

import json
import io
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from scripts import update_repo_stats as stats


REPO = "YuhaoYeSteve/embodied-data-generator"
STAMP = "2026-10-07T09:00:00Z"


def response(stars=0):
    return {"data": {"repository": {
        "nameWithOwner": REPO,
        "stargazerCount": stars,
        "forkCount": 0,
        "watchers": {"totalCount": 0},
        "issues": {"totalCount": 3},
        "pullRequests": {"totalCount": 2},
    }}}


class RepoStatsTests(unittest.TestCase):
    def test_zero_count_is_real_and_issues_exclude_pull_requests(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        self.assertEqual(snapshot["stars"], 0)
        self.assertEqual(snapshot["issues"], 3)
        self.assertEqual(snapshot["pull_requests"], 2)
        self.assertEqual(snapshot["watchers"], 0)

    def test_whitelists_metadata_and_does_not_persist_tokens(self):
        payload = response()
        payload["data"]["repository"]["token"] = "example-private-token-do-not-store"
        payload["authorization"] = "Bearer example-private-token-do-not-store"
        snapshot = stats.parse_snapshot(REPO, payload, STAMP)
        with tempfile.TemporaryDirectory() as tmp:
            stats.write_assets(Path(tmp), snapshot)
            text = "\n".join(p.read_text() for p in Path(tmp).iterdir())
        self.assertNotIn("private-token", text)
        self.assertNotIn("Bearer", text)

    def test_rejects_unsafe_repository_names_before_calling_github(self):
        invalid = ["owner/repo/extra", "https://github.com/a/b", "a/b?token=x",
                   "a/b\n", "a/$(env)", "../repo", "owner/..", "-owner/repo", ""]
        with patch.object(stats.subprocess, "run") as run:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    stats.fetch_snapshot(value, STAMP)
            run.assert_not_called()

    def test_missing_mismatched_negative_and_noninteger_counts_fail_closed(self):
        cases = []
        payload = response()
        del payload["data"]["repository"]["watchers"]
        cases.append(payload)
        for value in [-1, "15", True]:
            payload = response()
            payload["data"]["repository"]["stargazerCount"] = value
            cases.append(payload)
        payload = response()
        payload["data"]["repository"]["nameWithOwner"] = "another/repository"
        cases.append(payload)
        cases.extend([{"errors": [{"message": "secret error"}]}, {"data": {"repository": None}}])
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                stats.parse_snapshot(REPO, payload, STAMP)

    def test_fetch_uses_graphql_watchers_and_separate_issue_pr_counts(self):
        with patch.object(stats.subprocess, "run") as run:
            run.side_effect = [SimpleNamespace(stdout=json.dumps(response())),
                               SimpleNamespace(stdout='{"workflow_runs": []}')]
            snapshot = stats.fetch_snapshot(REPO, STAMP)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:3], ["gh", "api", "graphql"])
        self.assertIn("watchers { totalCount }", " ".join(command))
        self.assertIn("issues(states: OPEN)", " ".join(command))
        self.assertIn("pullRequests(states: OPEN)", " ".join(command))
        self.assertEqual(snapshot["issues"], 3)

    def test_ci_404_means_not_run_but_other_api_failures_are_not_hidden(self):
        with patch.object(stats.subprocess, "run", side_effect=subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: Not Found (HTTP 404)")):
            self.assertEqual(stats.fetch_ci(REPO)["label"], "not run")
        with patch.object(stats.subprocess, "run", side_effect=subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: rate limit (HTTP 403)")):
            with self.assertRaises(RuntimeError):
                stats.fetch_ci(REPO)

    def test_ci_maps_actual_run_and_never_treats_pending_as_passed(self):
        run = {
            "head_branch": "main", "head_repository": {"full_name": REPO},
            "event": "push", "status": "completed", "conclusion": "success",
            "html_url": f"https://github.com/{REPO}/actions/runs/123",
            "head_sha": "a" * 40, "token": "must-not-be-stored",
        }
        for state, conclusion, expected in [
            ("queued", None, "pending"), ("in_progress", None, "pending"),
            ("completed", "success", "passed"), ("completed", "failure", "failed"),
            ("completed", "cancelled", "cancelled"), ("completed", "skipped", "skipped"),
        ]:
            run.update(status=state, conclusion=conclusion)
            result = stats.parse_ci(REPO, {"workflow_runs": [run]})
            self.assertEqual(result["label"], expected)
            self.assertEqual(result["head_sha"], "a" * 40)
            self.assertNotIn("must-not-be-stored", json.dumps(result))
        run["event"] = "pull_request"
        self.assertEqual(stats.parse_ci(REPO, {"workflow_runs": [run]})["label"], "not run")
        run["event"] = "push"
        run["head_repository"] = {"full_name": "someone/fork"}
        self.assertEqual(stats.parse_ci(REPO, {"workflow_runs": [run]})["label"], "not run")

    def test_ci_failure_does_not_overwrite_previous_good_assets(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            stats.write_assets(target, snapshot)
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            with patch("sys.argv", ["stats", "--repo", REPO, "--output", tmp]), \
                    patch.object(stats, "fetch_ci", side_effect=RuntimeError("private error")), \
                    patch.object(stats.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(response()))):
                diagnostic = io.StringIO()
                with redirect_stderr(diagnostic), self.assertRaises(SystemExit) as exit_context:
                    stats.main()
                self.assertNotIn("private error", diagnostic.getvalue())
            self.assertEqual(exit_context.exception.code, 1)
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})

    def test_history_starts_with_one_zero_observation(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        history = stats.update_history(None, snapshot)
        self.assertEqual(history["samples"], [{"date": "2026-10-07", "stars": 0}])

    def test_daily_sample_is_replaced_and_stars_can_decrease(self):
        first = stats.parse_snapshot(REPO, response(12), "2026-10-06T09:00:00Z")
        history = stats.update_history(None, first)
        second = stats.parse_snapshot(REPO, response(10), STAMP)
        history = stats.update_history(history, second)
        latest = stats.parse_snapshot(REPO, response(9), "2026-10-07T21:00:00Z")
        history = stats.update_history(history, latest)
        self.assertEqual(history["samples"], [
            {"date": "2026-10-06", "stars": 12}, {"date": "2026-10-07", "stars": 9}])

    def test_rejects_history_from_other_repository_or_future_date(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        for history in [
            {"repository": "another/repo", "samples": []},
            {"repository": REPO, "samples": [{"date": "2026-10-08", "stars": 42}]},
            {"repository": REPO, "samples": [{"date": "2026-02-31", "stars": 42}]},
            {"repository": REPO, "samples": [{"date": "2026-10-06", "stars": -1}]},
        ]:
            with self.subTest(history=history), self.assertRaises(ValueError):
                stats.update_history(history, snapshot)

    def test_single_point_chart_is_valid_svg_with_honest_source_and_no_trend_line(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        history = stats.update_history(None, snapshot)
        svg = stats.render_history(history)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("2026-10-07", svg)
        self.assertIn("GitHub API", svg)
        self.assertIn("Daily snapshots", svg)
        self.assertIn("First observation", svg)
        self.assertEqual(len(root.findall('.//{http://www.w3.org/2000/svg}polyline')), 0)

    def test_asset_generation_keeps_real_history_and_only_expected_files(self):
        snapshot = stats.parse_snapshot(REPO, response(), STAMP)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            stats.write_assets(target, snapshot)
            self.assertEqual({p.name for p in target.iterdir()}, {
                "stats.json", "history.json", "stars.svg", "forks.svg", "watchers.svg",
                "issues.svg", "pull-requests.svg", "star-history.svg", "ci.svg"})
            stats.write_assets(target, snapshot)
            history = json.loads((target / "history.json").read_text())
            self.assertEqual(len(history["samples"]), 1)
            for path in target.glob("*.svg"):
                ET.parse(path)


if __name__ == "__main__":
    unittest.main()
