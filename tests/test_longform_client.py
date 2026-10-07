"""Offline contract tests; no Ark task is created by this suite."""
from __future__ import annotations

import copy
import base64
import importlib
import io
import random
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


try:
    client_module = importlib.import_module("longform_client")
except ModuleNotFoundError as exc:
    if exc.name != "longform_client":
        raise
    client_module = None


class SubmitArgvTests(unittest.TestCase):
    def test_known_model_contract_rejects_invalid_combinations_before_cli(self):
        for model, resolution, duration in [
            ('doubao-seedance-2-0-260128', '720p', 30),
            ('doubao-seedance-2-5-260628', '4k', 15),
        ]:
            client=client_module.ArkCLIClient('profile',model)
            body=dict(self.body,model=model,resolution=resolution,duration=duration)
            result=subprocess.CompletedProcess(['arkcli'],0,stdout='{"id":"cgt-fixture","status":"queued"}',stderr='')
            with self.subTest(model=model), patch('subprocess.run',return_value=result) as run:
                with self.assertRaises(ValueError): client.submit(body)
                run.assert_not_called()

    def test_model_20_4k_request_keeps_real_model_and_resolution(self):
        model='doubao-seedance-2-0-260128'
        client=client_module.ArkCLIClient('profile',model)
        body=dict(self.body,model=model,resolution='4k',duration=15)
        argv=client.build_submit_argv(body)
        self.assertEqual(argv[argv.index('--resolution')+1],'4k')
        self.assertEqual(json.loads(argv[argv.index('--extra-body')+1])['model'],model)

    def setUp(self):
        self.assertIsNotNone(client_module, "longform_client must implement the CLI backend")
        self.client = client_module.ArkCLIClient("ego-profile", "ep-fixture")
        self.body = {
            "model": "ep-fixture",
            "content": [
                {"type": "text", "text": "第一视角行走；literal $(touch nope) `whoami`"},
                {
                    "type": "video_url",
                    "video_url": {"url": "https://example.invalid/clip.mp4?signature=private"},
                    "role": "reference_video",
                },
            ],
            "duration": 15,
            "ratio": "16:9",
            "resolution": "720p",
            "generate_audio": True,
            "return_last_frame": True,
            "seed": 1234,
            "watermark": False,
        }

    def test_preserves_complete_request_as_json_and_literal_prompt(self):
        original = copy.deepcopy(self.body)
        argv = self.client.build_submit_argv(self.body)
        self.assertEqual(argv[:3], ["arkcli", "+gen", self.body["content"][0]["text"]])
        self.assertEqual(json.loads(argv[argv.index("--extra-body") + 1]), self.body)
        self.assertEqual(self.body, original)
        for flag, value in [
            ("--model", "ep-fixture"),
            ("--modality", "video"),
            ("--profile", "ego-profile"),
            ("--duration", "15"),
            ("--ratio", "16:9"),
            ("--resolution", "720p"),
            ("--save-to", ""),
            ("--format", "json"),
        ]:
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--generate-audio", argv)
        self.assertIn("--return-last-frame", argv)
        self.assertIn("--no-open", argv)

    def test_false_boolean_fields_are_forwarded_explicitly(self):
        self.body.update(generate_audio=False, return_last_frame=False)
        argv = self.client.build_submit_argv(self.body)
        self.assertIn("--generate-audio=false", argv)
        self.assertIn("--return-last-frame=false", argv)

    def test_model_mismatch_is_rejected_before_submission(self):
        self.body["model"] = "ep-other"
        with self.assertRaisesRegex(ValueError, "model"):
            self.client.build_submit_argv(self.body)

    def test_required_parameters_are_validated_locally(self):
        for field, value in [("duration", 0), ("duration", True), ("duration", 1.5),
                             ("generate_audio", "false"), ("return_last_frame", None),
                             ("ratio", ""), ("resolution", "")]:
            body = copy.deepcopy(self.body)
            body[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.client.build_submit_argv(body)

    def test_missing_request_fields_are_not_filled_with_unrecorded_defaults(self):
        for field in ["duration", "ratio", "resolution", "generate_audio", "return_last_frame"]:
            body = copy.deepcopy(self.body)
            del body[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.client.build_submit_argv(body)

    def test_missing_model_uses_client_model_in_forwarded_body(self):
        del self.body["model"]
        argv = self.client.build_submit_argv(self.body)
        wire = json.loads(argv[argv.index("--extra-body") + 1])
        self.assertEqual(wire["model"], "ep-fixture")
        self.assertNotIn("model", self.body)

    def test_textless_or_multiple_text_requests_are_rejected(self):
        for content in [[], [{"type": "text", "text": " "}],
                        [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]]:
            body = dict(self.body, content=content)
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "text"):
                self.client.build_submit_argv(body)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(client_module, "longform_client must implement the CLI backend")
        for name in ["ArkCLIError", "SubmissionUnknown"]:
            self.assertTrue(hasattr(client_module, name), f"missing exception contract: {name}")
        for name in ["submit", "get"]:
            self.assertTrue(hasattr(client_module.ArkCLIClient, name), f"missing client method: {name}")
        self.client = client_module.ArkCLIClient("ego-profile", "ep-fixture", timeout=30)
        self.body = {
            "model": "ep-fixture",
            "content": [{"type": "text", "text": "first-person walk"}],
            "duration": 15, "ratio": "16:9", "resolution": "720p",
            "generate_audio": True, "return_last_frame": True,
        }

    def completed(self, payload, *, code=0, stderr="", prefix=""):
        return subprocess.CompletedProcess(
            ["arkcli"], code, stdout=prefix + json.dumps(payload), stderr=stderr)

    def test_submit_preserves_raw_response_and_all_task_metadata(self):
        payload = {"data": {"id": "cgt-fixture", "status": "queued", "model": "ep-fixture"},
                   "request_id": "req-fixture", "cli": {"profile": "ego-profile"}}
        with patch("subprocess.run", return_value=self.completed(payload)) as run:
            result = self.client.submit(self.body)
        self.assertEqual(result["id"], "cgt-fixture")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["raw"], payload)
        call = run.call_args
        self.assertEqual(call.args[0], self.client.build_submit_argv(self.body))
        self.assertIs(call.kwargs["shell"], False)
        self.assertIs(call.kwargs["capture_output"], True)
        self.assertIs(call.kwargs["text"], True)
        self.assertIs(call.kwargs["check"], False)
        self.assertEqual(call.kwargs.get("encoding"), "utf-8")
        self.assertEqual(call.kwargs.get("errors"), "replace")
        self.assertEqual(call.kwargs["timeout"], 30)
        for key, value in {
            "ARKCLI_NO_UPDATE_NOTIFIER": "1", "ARKCLI_CALLER_TYPE": "ai_agent",
            "ARKCLI_CALLER_NAME": "codex", "ARKCLI_SKILL_NAME": "arkcli-gen",
        }.items():
            self.assertEqual(call.kwargs["env"][key], value)
        self.assertEqual(call.kwargs["env"].get("PATH"), os.environ.get("PATH"))
        self.assertEqual(run.call_count, 1)

    def test_get_preserves_fields_in_a_synthetic_cli_envelope(self):
        task = {"id": "cgt-fixture", "status": "succeeded", "usage": {
            "completion_tokens": 123456, "total_tokens": 123456,
        }, "content": {"video_url": "https://example.invalid/out.mp4?sig=private",
                       "last_frame_url": "https://example.invalid/frame.png?sig=private"},
                "created_at": 1700000000, "updated_at": 1700000060}
        payload = {"data": {"task": task}, "request_id": "req-get"}
        with patch("subprocess.run", return_value=self.completed(payload)) as run:
            result = self.client.get("cgt-fixture")
        self.assertEqual(result["usage"], task["usage"])
        self.assertEqual(result["content"], task["content"])
        self.assertEqual(result["raw"], payload)
        self.assertEqual(run.call_args.args[0], [
            "arkcli", "gen", "get", "cgt-fixture", "--profile", "ego-profile",
            "--save-to", "", "--no-open", "--format", "json",
        ])

    def test_confirmed_cli_get_shape_preserves_output_usage_and_raw_cli_response(self):
        payload = {
            "id": "cgt-fixture", "model": "ep-fixture",
            "status": {"phase": "succeeded"},
            "output_url": "https://example.invalid/video.mp4?signature=private",
            "ratio": "16:9", "resolution": "720p", "duration": 4,
            "generate_audio": True,
            "usage": {"completion_tokens": 87300, "total_tokens": 87300},
        }
        with patch("subprocess.run", return_value=self.completed(payload)):
            result = self.client.get("cgt-fixture")
        self.assertEqual(result["id"], payload["id"])
        self.assertEqual(result["status"], "succeeded")
        for field in ["model", "output_url", "ratio", "resolution", "duration",
                      "generate_audio", "usage"]:
            self.assertEqual(result[field], payload[field])
        self.assertEqual(result["raw"], payload)
        self.assertEqual(result["raw"]["status"], {"phase": "succeeded"})
        self.assertNotIn("last_frame_url", result)
        self.assertNotIn("content", result)

    def test_supported_task_envelopes_and_id_alias(self):
        for payload in [{"id": "cgt-fixture"}, {"task_id": "cgt-fixture"},
                        {"task": {"id": "cgt-fixture"}},
                        {"data": {"task_id": "cgt-fixture"}}]:
            with self.subTest(payload=payload), patch(
                    "subprocess.run", return_value=self.completed(payload)):
                result = self.client.submit(self.body)
                self.assertEqual(result["id"], "cgt-fixture")
                self.assertEqual(result["raw"], payload)

    def test_confirmed_cli_submit_shape_prefers_top_level_status(self):
        payload = {"kind": "video", "model": "ep-fixture", "prompt": "walk",
                   "task_id": "cgt-fixture", "status": "queued", "task": {
                       "id": "cgt-fixture", "model": "ep-fixture",
                       "status": {"phase": "queued"}}}
        with patch("subprocess.run", return_value=self.completed(payload)):
            result = self.client.submit(self.body)
        self.assertEqual(result["id"], "cgt-fixture")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["raw"], payload)

    def test_nested_phase_status_is_normalized(self):
        payload = {"task": {"id": "cgt-fixture", "status": {"phase": "running"}}}
        with patch("subprocess.run", return_value=self.completed(payload)):
            self.assertEqual(self.client.get("cgt-fixture")["status"], "running")

    def test_leading_notice_lines_do_not_break_json_decoding(self):
        prefix = 'Notice: using profile {ego-profile}\n[INFO] update notifier disabled\n'
        with patch("subprocess.run", return_value=self.completed(
                {"id": "cgt-fixture"}, prefix=prefix)):
            self.assertEqual(self.client.submit(self.body)["id"], "cgt-fixture")

    def test_invalid_and_ambiguous_submit_outputs_are_unknown_without_retry(self):
        outputs = ["not JSON", "{}", "[]", '{"id":"cgt-fixture"}\ntrailing notice',
                   '{"notice":true}\n{"id":"cgt-fixture"}',
                   '{"result":{"id":"cgt-hidden"}}']
        for stdout in outputs:
            with self.subTest(stdout=stdout), patch("subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, stdout, "")) as run:
                with self.assertRaises(client_module.SubmissionUnknown):
                    self.client.submit(self.body)
                self.assertEqual(run.call_count, 1)

    def test_submit_timeout_is_unknown_and_does_not_disclose_argv(self):
        exc = subprocess.TimeoutExpired(
            ["arkcli", "+gen", "private prompt", "https://example.invalid/?sig=secret"],
            30, stderr=b"waiting for response")
        with patch("subprocess.run", side_effect=exc) as run:
            with self.assertRaises(client_module.SubmissionUnknown) as raised:
                self.client.submit(self.body)
        self.assertIn("waiting for response", str(raised.exception))
        self.assertNotIn("private prompt", str(raised.exception))
        self.assertNotIn("sig=secret", str(raised.exception))
        self.assertEqual(run.call_count, 1)

    def test_get_timeout_is_query_error_not_submission_unknown(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired([], 30)):
            with self.assertRaises(client_module.ArkCLIError) as raised:
                self.client.get("cgt-fixture")
        self.assertNotIsInstance(raised.exception, client_module.SubmissionUnknown)

    def test_failed_task_is_returned_with_server_error_and_usage(self):
        payload = {"id": "cgt-fixture", "status": "failed",
                   "error": {"code": "InternalServerError", "message": "generation failed"},
                   "usage": {"total_tokens": 100}}
        with patch("subprocess.run", return_value=self.completed(payload)):
            try:
                result = self.client.get("cgt-fixture")
            except client_module.ArkCLIError:
                self.fail("a failed task must return its server metadata rather than a query error")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], payload["error"])
        self.assertEqual(result["usage"], payload["usage"])
        self.assertEqual(result["raw"], payload)

    def test_invalid_query_output_is_query_error(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "bad", "")):
            with self.assertRaises(client_module.ArkCLIError) as raised:
                self.client.get("cgt-fixture")
        self.assertNotIsInstance(raised.exception, client_module.SubmissionUnknown)

    def test_missing_executable_is_definite_local_failure(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("arkcli not found")):
            with self.assertRaises(client_module.ArkCLIError) as raised:
                self.client.submit(self.body)
        self.assertNotIsInstance(raised.exception, client_module.SubmissionUnknown)

    def test_cli_argument_error_is_definite(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 2, "", "unknown flag: --unsupported")):
            with self.assertRaises(client_module.ArkCLIError) as raised:
                self.client.submit(self.body)
        self.assertNotIsInstance(raised.exception, client_module.SubmissionUnknown)
        self.assertIn("unknown flag", str(raised.exception))
        self.assertNotIn("submission outcome unknown", str(raised.exception))

    def test_explicit_server_rejection_is_definite_and_diagnostics_are_redacted(self):
        payload = {"error": {"code": "InvalidParameter", "message": "bad duration"}}
        stderr = ('Authorization: Bearer abc.def.secret ARK_API_KEY=fixture-secret '
                  '"api_key": "json-secret" token=token-secret sk-fakesecret123 '
                  'https://user:pass@example.invalid/media.mp4?signature=signed-secret#token-fragment')
        with patch("subprocess.run", return_value=self.completed(payload, code=1, stderr=stderr)):
            with self.assertRaises(client_module.ArkCLIError) as raised:
                self.client.submit(self.body)
        self.assertNotIsInstance(raised.exception, client_module.SubmissionUnknown)
        message = str(raised.exception)
        self.assertIn("InvalidParameter", message)
        self.assertIn("bad duration", message)
        self.assertIn("example.invalid/media.mp4", message)
        for secret in ["abc.def.secret", "fixture-secret", "json-secret", "token-secret",
                       "sk-fakesecret123", "signed-secret", "token-fragment", "user:pass"]:
            self.assertNotIn(secret, message)

    def test_unclassified_nonzero_exit_is_unknown(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, "", "connection reset after send")):
            with self.assertRaises(client_module.SubmissionUnknown):
                self.client.submit(self.body)

    def test_server_timeout_or_internal_error_is_unknown(self):
        for code in ["RequestTimeout", "InternalServerError", "ServiceUnavailable"]:
            with self.subTest(code=code), patch("subprocess.run", return_value=self.completed(
                    {"error": {"code": code, "message": "temporary failure"}}, code=1)):
                with self.assertRaises(client_module.SubmissionUnknown):
                    self.client.submit(self.body)

    def test_nonzero_exit_with_created_task_retains_id(self):
        payload = {"id": "cgt-fixture", "status": "queued"}
        with patch("subprocess.run", return_value=self.completed(
                payload, code=1, stderr="post-submit local failure")):
            self.assertEqual(self.client.submit(self.body)["id"], "cgt-fixture")

    def test_invalid_client_configuration_and_task_ids_are_rejected(self):
        for profile, model, timeout in [("", "ep-fixture", 30), ("profile", "", 30),
                                        ("profile", "ep-fixture", 0),
                                        ("profile", "ep-fixture", float("inf"))]:
            with self.subTest(profile=profile, model=model, timeout=timeout), self.assertRaises(ValueError):
                client_module.ArkCLIClient(profile, model, timeout=timeout)
        for task_id in ["", " ", None, "--help", "cgt-valid\nother"]:
            with self.subTest(task_id=task_id), patch("subprocess.run") as run:
                with self.assertRaises(ValueError):
                    self.client.get(task_id)
                run.assert_not_called()


class InlineMediaTests(unittest.TestCase):
    """Large inline images must cross the process boundary as files, not argv JSON."""

    @classmethod
    def setUpClass(cls):
        from PIL import Image
        # Incompressible synthetic pixels exercise the large-argv regression
        # without depending on any user's image or checked-in binary fixture.
        rng = random.Random(20261007)
        pixels = bytes(rng.getrandbits(8) for _ in range(640 * 640 * 3))
        buffer = io.BytesIO()
        Image.frombytes("RGB", (640, 640), pixels).save(buffer, format="PNG")
        cls.png = buffer.getvalue()
        cls.png_uri = "data:image/png;base64," + base64.b64encode(cls.png).decode("ascii")

    def setUp(self):
        self.client = client_module.ArkCLIClient("ego-profile", "ep-fixture", timeout=30)
        self.body = {
            "model": "ep-fixture",
            "content": [
                {"type": "text", "text": "使用图片中的订单确认页面；literal $(touch nope)"},
                {"type": "video_url", "video_url": {
                    "url": "https://example.invalid/previous.mp4?signature=private"},
                 "role": "reference_video"},
                {"type": "image_url", "image_url": {"url": self.png_uri},
                 "role": "reference_image"},
                {"type": "audio_url", "audio_url": {"url": "https://example.invalid/beat.mp3"},
                 "role": "reference_audio"},
            ],
            "duration": 15, "ratio": "16:9", "resolution": "720p",
            "generate_audio": True, "return_last_frame": True,
            "seed": 1234, "watermark": False, "service_tier": "default",
            "custom_server_field": {"preserve": [1, 2, 3]},
        }

    @staticmethod
    def inputs(argv):
        return [argv[index + 1] for index, value in enumerate(argv) if value == "--input"]

    def test_synthetic_large_png_uses_short_argv_with_exact_bytes_role_and_media_order(self):
        original = copy.deepcopy(self.body)
        observed_files = []

        def run(argv, **kwargs):
            self.assertGreater(len(self.png), 900_000)
            self.assertLess(sum(len(arg.encode("utf-8")) + 1 for arg in argv), 16_384)
            self.assertFalse(any("data:image" in arg for arg in argv))
            extra = json.loads(argv[argv.index("--extra-body") + 1])
            self.assertNotIn("content", extra, "extra-body must not overwrite CLI file inputs")
            self.assertEqual(extra, {key: value for key, value in original.items() if key != "content"})
            inputs = self.inputs(argv)
            self.assertEqual(len(inputs), 3)
            self.assertEqual(inputs[0], "reference_video:" + original["content"][1]["video_url"]["url"])
            self.assertTrue(inputs[1].startswith("reference_image:@"))
            staged = Path(inputs[1].removeprefix("reference_image:@"))
            observed_files.append(staged)
            self.assertEqual(staged.suffix, ".png")
            self.assertEqual(staged.read_bytes(), self.png)
            self.assertEqual(inputs[2], "reference_audio:https://example.invalid/beat.mp3")
            # Reconstruct the documented CLI content contract while its input exists.
            reconstructed = [
                {"type": "text", "text": argv[2]},
                copy.deepcopy(original["content"][1]),
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                    base64.b64encode(staged.read_bytes()).decode("ascii")},
                 "role": inputs[1].split(":", 1)[0]},
                copy.deepcopy(original["content"][3]),
            ]
            self.assertEqual(reconstructed, original["content"])
            return subprocess.CompletedProcess(argv, 0, '{"id":"cgt-image"}', "")

        with patch("subprocess.run", side_effect=run) as mocked:
            self.assertEqual(self.client.submit(self.body)["id"], "cgt-image")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.body, original)
        self.assertEqual(len(observed_files), 1)
        self.assertFalse(observed_files[0].exists())
        self.assertFalse(observed_files[0].parent.exists())

    def test_multiple_inline_images_and_audio_keep_explicit_roles_and_bytes(self):
        audio = b"ID3\x04\x00\x00\x00\x00\x00\x00fixture-audio"
        self.body["content"] = [self.body["content"][0],
            {"type": "image_url", "image_url": {"url": self.png_uri}, "role": "first_frame"},
            {"type": "image_url", "image_url": {"url": self.png_uri}, "role": "reference_image"},
            {"type": "audio_url", "audio_url": {"url": "data:audio/mpeg;base64," +
                base64.b64encode(audio).decode("ascii")}, "role": "reference_audio"}]

        def run(argv, **kwargs):
            inputs = self.inputs(argv)
            self.assertEqual([value.split(":", 1)[0] for value in inputs],
                             ["first_frame", "reference_image", "reference_audio"])
            paths = [Path(value.split(":@", 1)[1]) for value in inputs]
            self.assertEqual(len(set(paths)), 3)
            self.assertEqual([path.read_bytes() for path in paths], [self.png, self.png, audio])
            self.assertEqual([path.suffix for path in paths], [".png", ".png", ".mp3"])
            return subprocess.CompletedProcess(argv, 0, '{"id":"cgt-image"}', "")

        with patch("subprocess.run", side_effect=run):
            self.client.submit(self.body)

    def test_staged_media_is_cleaned_after_rejection_unknown_timeout_and_launch_failure(self):
        outcomes = [
            ("rejected", subprocess.CompletedProcess([], 1,
                '{"error":{"code":"InvalidParameter","message":"bad input"}}', ""),
             client_module.ArkCLIError),
            ("unknown", subprocess.CompletedProcess([], 0, "{}", ""),
             client_module.SubmissionUnknown),
            ("bad_json", subprocess.CompletedProcess([], 0, "not json", ""),
             client_module.SubmissionUnknown),
            ("timeout", subprocess.TimeoutExpired([], 30), client_module.SubmissionUnknown),
            ("launch_failure", FileNotFoundError("arkcli unavailable"), client_module.ArkCLIError),
        ]
        for name, outcome, error_type in outcomes:
            staged_paths = []

            def run(argv, **kwargs):
                staged_paths.extend(Path(value.split(":@", 1)[1])
                                    for value in self.inputs(argv) if ":@" in value)
                self.assertEqual(len(staged_paths), 1)
                self.assertTrue(staged_paths[0].is_file())
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with self.subTest(outcome=name), patch("subprocess.run", side_effect=run) as mocked:
                with self.assertRaises(error_type):
                    self.client.submit(self.body)
                self.assertEqual(mocked.call_count, 1)
                self.assertFalse(staged_paths[0].exists())
                self.assertFalse(staged_paths[0].parent.exists())

    def test_unrepresentable_content_is_rejected_without_execution_or_leftover_files(self):
        changes = [
            ("item metadata", lambda body: body["content"][2].update(detail="high")),
            ("nested metadata", lambda body: body["content"][2]["image_url"].update(detail="high")),
            ("text metadata", lambda body: body["content"][0].update(role="user")),
            ("invalid role", lambda body: body["content"][2].update(role="reference_video")),
            ("missing role", lambda body: body["content"][2].pop("role")),
            ("untyped remote video", lambda body: body["content"][1]["video_url"].update(
                url="https://example.invalid/download?id=video.mp4")),
            ("remote type mismatch", lambda body: body["content"][1]["video_url"].update(
                url="https://example.invalid/sound.mp3")),
            ("unsupported type", lambda body: body["content"].append(
                {"type": "file_url", "file_url": {"url": "https://example.invalid/file.pdf"}})),
            ("text moved", lambda body: body["content"].append(body["content"].pop(0))),
            ("bad base64", lambda body: body["content"][2]["image_url"].update(
                url="data:image/png;base64,not!base64")),
            ("empty image", lambda body: body["content"][2]["image_url"].update(
                url="data:image/png;base64,")),
            ("non-base64 data", lambda body: body["content"][2]["image_url"].update(
                url="data:image/png,raw-image")),
            ("unknown MIME", lambda body: body["content"][2]["image_url"].update(
                url="data:image/x-custom;base64,aW1hZ2U=")),
            ("MIME mismatch", lambda body: body["content"][2]["image_url"].update(
                url="data:audio/mpeg;base64,aW1hZ2U=")),
            ("embedded extra data", lambda body: body.update(
                custom_server_field={"hidden": self.png_uri})),
        ]
        for name, change in changes:
            body = copy.deepcopy(self.body)
            change(body)
            with self.subTest(case=name), tempfile.TemporaryDirectory() as parent:
                with patch("tempfile.tempdir", parent), patch("subprocess.run", return_value=
                        subprocess.CompletedProcess([], 0, '{"id":"cgt-should-not-submit"}', "")) as run:
                    with self.assertRaises(ValueError) as raised:
                        self.client.submit(body)
                    run.assert_not_called()
                self.assertEqual(list(Path(parent).iterdir()), [])
                self.assertNotIn(self.png_uri, str(raised.exception))

    def test_inline_image_and_audio_are_redacted_from_error_message_and_stderr(self):
        secrets = [self.png_uri, "data:audio/mpeg;base64," + "cHJpdmF0ZQ==" * 100]
        for uri in secrets:
            payload = {"error": {"code": "InvalidParameter", "message": "invalid media " + uri}}
            with self.subTest(media=uri.split(";", 1)[0]), patch("subprocess.run", return_value=
                    subprocess.CompletedProcess([], 1, json.dumps(payload), "rejected " + uri)):
                with self.assertRaises(client_module.ArkCLIError) as raised:
                    self.client.submit(self.body)
            self.assertIn("[REDACTED", str(raised.exception))
            self.assertLess(len(raised.exception.stderr), 1000)
            self.assertNotIn(uri.split(",", 1)[1][:100], str(raised.exception))
            self.assertNotIn(uri.split(",", 1)[1][:100], raised.exception.stderr)


if __name__ == "__main__":
    unittest.main()
