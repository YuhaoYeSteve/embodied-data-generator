"""Offline tests for direct Responses quality review; no service calls."""
import base64
import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

try:
    review = importlib.import_module("review_api")
except ModuleNotFoundError as exc:
    if exc.name != "review_api":
        raise
    review = None


class FakeResponse(io.BytesIO):
    status = 200


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(review, "review_api must provide the direct Responses adapter")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "review.json"
        self.config = self.root / "config.yaml"
        self.key = "fixture-selected-private-key"
        self.video = "https://example.invalid/clip.mp4?signature=private-signature"
        self.config.write_text(
            "profiles:\n  unrelated:\n    api_key: unrelated-key\n"
            "  review-test:\n    type: platform\n"
            "    region: cn-beijing\n    api_key: " + self.key + "\n"
            "    available_api_keys:\n      - " + self.key + "\n"
            "  later:\n    api_key: later-key\n")
        self.config_patch = patch.object(review, "CONFIG_PATH", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        for name, value in {"PROFILE": "review-test", "DEFAULT_MODEL": "ep-review-fixture",
                            "VERIFIED_ENDPOINTS": frozenset({"ep-review-fixture"})}.items():
            configured = patch.object(review, name, value)
            configured.start()
            self.addCleanup(configured.stop)
        self.response = {
            "id": "resp-fixture", "status": "completed", "service_tier": "new-tier",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private reasoning"}]},
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "画面连续。"},
                    {"type": "output_text", "text": "音频清晰。"},
                ]},
            ],
            "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
        }

    def fake_open(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(json.dumps(self.response).encode())

    def test_body_uses_exact_responses_video_wire_and_fixed_endpoint(self):
        with patch.object(review, "_open_request", side_effect=self.fake_open) as send:
            result = review.run_review([self.video], "审核音视频", self.output)
        body = json.loads(self.request.data)
        self.assertEqual(self.request.full_url, "https://ark.cn-beijing.volces.com/api/v3/responses")
        self.assertEqual(self.request.get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual(body["model"], "ep-review-fixture")
        self.assertEqual(body["input"], [{"role": "user", "content": [
            {"type": "input_video", "video_url": self.video},
            {"type": "input_text", "text": "审核音视频"},
        ]}])
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_output_tokens"], 1800)
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], False)
        self.assertEqual(self.timeout, 120)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result, self.response)

    def test_optional_wav_is_embedded_as_audio_url(self):
        audio = self.root / "voice.wav"
        audio.write_bytes(b"RIFF-fixture-WAVE")
        with patch.object(review, "_open_request", side_effect=self.fake_open):
            review.run_review([self.video], "compare", self.output, audio_paths=[audio])
        content = json.loads(self.request.data)["input"][0]["content"]
        self.assertEqual(content[1], {
            "type": "input_audio",
            "audio_url": "data:audio/wav;base64," + base64.b64encode(audio.read_bytes()).decode(),
        })
        self.assertNotIn(base64.b64encode(audio.read_bytes()).decode(), self.output.read_text())

    def test_audio_only_review_does_not_duplicate_video_timeline(self):
        audio = self.root / "voice.wav"
        audio.write_bytes(b"RIFF-fixture-WAVE")
        with patch.object(review, "_open_request", side_effect=self.fake_open):
            review.run_review([], "transcribe only", self.output, audio_paths=[audio])
        content = json.loads(self.request.data)["input"][0]["content"]
        self.assertEqual([part['type'] for part in content],['input_audio','input_text'])

    def test_saved_record_contains_text_usage_and_hashes_without_secrets(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(review, "_open_request", side_effect=self.fake_open), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            review.run_review([self.video], "审核", self.output)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["text"], "画面连续。音频清晰。")
        self.assertEqual(record["response"], self.response)
        self.assertEqual(record["usage"], self.response["usage"])
        self.assertEqual(record["request_summary"]["video_count"], 1)
        self.assertEqual(len(record["request_sha256"]), 64)
        self.assertTrue(record["started_at"])
        self.assertTrue(record["finished_at"])
        saved = self.output.read_text() + stdout.getvalue() + stderr.getvalue()
        for secret in [self.key, self.video, "private-signature", "Authorization", "unrelated-key", "later-key"]:
            self.assertNotIn(secret, saved)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_extract_text_ignores_reasoning(self):
        self.assertEqual(review.extract_text(self.response), "画面连续。音频清晰。")

    def test_http_error_is_saved_redacted_and_not_retried(self):
        error_body = {"error": {"message": "bad " + self.key + " " + self.video}}
        exc = HTTPError(review.BASE_URL + "/responses", 400, "bad", {},
                        io.BytesIO(json.dumps(error_body).encode()))
        with patch.object(review, "_open_request", side_effect=exc) as send:
            with self.assertRaises(review.ReviewError) as raised:
                review.run_review([self.video], "review", self.output)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["http_status"], 400)
        saved = self.output.read_text() + str(raised.exception)
        self.assertNotIn(self.key, saved)
        self.assertNotIn("private-signature", saved)
        self.assertEqual(send.call_count, 1)

    def test_timeout_records_unknown_outcome_without_retry(self):
        with patch.object(review, "_open_request", side_effect=TimeoutError("timeout")) as send:
            with self.assertRaises(review.ReviewError):
                review.run_review([self.video], "review", self.output)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["status"], "unknown")
        self.assertIsNone(record["usage"])
        self.assertEqual(send.call_count, 1)

    def test_echoed_secrets_urls_and_headers_are_removed_from_returned_response(self):
        self.response["echo"] = {"url": self.video, "key": self.key,
                                 "headers": {"Authorization": "Bearer " + self.key}}
        with patch.object(review, "_open_request", side_effect=self.fake_open):
            result = review.run_review([self.video], "review", self.output)
        serialized = json.dumps(result) + self.output.read_text()
        self.assertNotIn(self.key, serialized)
        self.assertNotIn(self.video, serialized)
        self.assertNotIn("Authorization", serialized)

    def test_missing_review_model_fails_before_loading_key_or_network(self):
        with patch.object(review, "DEFAULT_MODEL", ""), \
                patch.object(review, "VERIFIED_ENDPOINTS", frozenset()), \
                patch.object(review, "_load_selected_key") as key, \
                patch.object(review, "_open_request") as send:
            with self.assertRaisesRegex(review.ReviewError, "review_model"):
                review.run_review([self.video], "review", self.output)
        key.assert_not_called()
        send.assert_not_called()

    def test_review_profile_must_match_fixed_platform_region_before_network(self):
        original = self.config.read_text()
        for modified in (original.replace("type: platform", "type: plan"),
                         original.replace("region: cn-beijing", "region: cn-shanghai")):
            with self.subTest(config=modified), patch.object(review, "_open_request") as send:
                self.config.write_text(modified)
                with self.assertRaisesRegex(review.ReviewError, "platform API"):
                    review.run_review([self.video], "review", self.output)
                send.assert_not_called()

    def test_unknown_model_rejected_before_network(self):
        with patch.object(review, "_open_request") as send:
            with self.assertRaises(review.ReviewError):
                review.run_review([self.video], "review", self.output, model="ep-unverified")
        send.assert_not_called()

    def test_incomplete_or_empty_response_is_not_reported_successful(self):
        for response in [{"id": "resp-fixture", "status": "incomplete", "output": []},
                         {"id": "resp-fixture", "status": "completed", "output": []}]:
            self.response = response
            with self.subTest(response=response), patch.object(review, "_open_request", side_effect=self.fake_open):
                with self.assertRaises(review.ReviewError):
                    review.run_review([self.video], "review", self.output)
            self.assertEqual(json.loads(self.output.read_text())["status"], "failed")

    def test_cli_accepts_repeated_generator_responses_and_prompt_file(self):
        first = self.root / "first.json"
        second = self.root / "second.json"
        prompt = self.root / "prompt.txt"
        first.write_text(json.dumps({"output_url": self.video}))
        second.write_text(json.dumps({"content": {"video_url": self.video + "2"}}))
        prompt.write_text("compare clips")
        with patch.object(review, "_open_request", side_effect=self.fake_open), contextlib.redirect_stdout(io.StringIO()):
            code = review.main(["--video-response", str(first), "--video-response", str(second),
                                "--prompt-file", str(prompt), "--output", str(self.output)])
        self.assertEqual(code, 0)
        content = json.loads(self.request.data)["input"][0]["content"]
        self.assertEqual([item["video_url"] for item in content if item["type"] == "input_video"],
                         [self.video, self.video + "2"])

    def test_cli_error_returns_nonzero_and_does_not_print_key(self):
        item, prompt = self.root / "item.json", self.root / "prompt.txt"
        item.write_text(json.dumps({"output_url": self.video}))
        prompt.write_text("review")
        stderr = io.StringIO()
        with patch.object(review, "_open_request", side_effect=TimeoutError(self.key)), contextlib.redirect_stderr(stderr):
            code = review.main(["--video-response", str(item), "--prompt-file", str(prompt), "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn(self.key, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
