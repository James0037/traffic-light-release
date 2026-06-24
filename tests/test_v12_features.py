import hashlib
import json
import queue
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import setup_installer
import stream
import update_config
import uninstall_helper
import updater


class V12FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def config(self):
        config = stream.DEFAULT_CONFIG.copy()
        config.update(
            {
                "image_dir": str(self.root / "frames"),
                "results_dir": str(self.root / "results"),
                "api_key": "sk-test-secret-123456",
                "rtsp_password": "camera-secret",
                "stream_url": "rtsp://admin:camera-secret@192.0.2.10/live",
            }
        )
        return config

    def test_v21_uses_independent_version_and_data_dir(self):
        self.assertEqual(stream.APP_VERSION, "2.1.1")
        self.assertIn("V2.1", str(stream.DATA_DIR))
        self.assertNotIn("V1.2_数据", str(stream.DATA_DIR))

    def test_parse_ffmpeg_duration(self):
        output = "Input #0, mov, from 'a.mp4':\n  Duration: 01:02:03.45, start: 0.000000"
        self.assertAlmostEqual(stream.parse_ffmpeg_duration(output), 3723.45)
        self.assertEqual(stream.parse_ffmpeg_duration("no duration"), 0.0)

    def test_preview_ffmpeg_command_for_local_seek(self):
        command = stream.build_preview_ffmpeg_command(
            "ffmpeg",
            "file",
            "demo.mp4",
            start_time=12.5,
            single_frame=True,
        )
        self.assertIn("-ss", command)
        self.assertIn("12.5", command)
        self.assertIn("-frames:v", command)
        self.assertEqual(command[-1], "pipe:1")

    def test_preview_ffmpeg_command_for_stream_uses_input_options(self):
        command = stream.build_preview_ffmpeg_command(
            "ffmpeg",
            "stream",
            "rtsp://example.com/live",
            input_options=["-rtsp_transport", "tcp"],
            fps=8,
        )
        self.assertNotIn("-ss", command)
        self.assertIn("-rtsp_transport", command)
        self.assertIn("fps=8", " ".join(command))

    def test_fresh_connection_defaults_and_template_are_blank(self):
        self.assertEqual(stream.DEFAULT_CONFIG["api_url"], "")
        self.assertEqual(stream.DEFAULT_CONFIG["api_key"], "")
        self.assertEqual(stream.DEFAULT_CONFIG["model"], "")
        self.assertEqual(stream.DEFAULT_CONFIG["ssh_api_path"], "")
        self.assertEqual(stream.DEFAULT_CONFIG["capture_mode"], "interval")
        self.assertEqual(stream.DEFAULT_CONFIG["capture_point_time"], "00:00:00")
        self.assertEqual(stream.DEFAULT_CONFIG["capture_start_time"], "00:00:00")
        self.assertEqual(stream.DEFAULT_CONFIG["capture_end_time"], "00:01:00")
        self.assertEqual(stream.DEFAULT_CONFIG["stream_first_frame_timeout"], 120)
        self.assertEqual(stream.DEFAULT_CONFIG["update_url"], stream.DEFAULT_UPDATE_INFO)
        self.assertEqual(stream.DEFAULT_UPDATE_INFO, update_config.OFFICIAL_UPDATE_URL)
        self.assertEqual(
            update_config.LOCAL_STATIC_UPDATE_URL,
            "http://127.0.0.1:8000/releases/latest/update.json",
        )

        template_path = Path(__file__).resolve().parents[1] / "stream_config.template.json"
        template = json.loads(template_path.read_text(encoding="utf-8"))
        self.assertEqual(template["api_url"], "")
        self.assertEqual(template["api_key"], "")
        self.assertEqual(template["model"], "")
        self.assertEqual(template["ssh_api_path"], "")
        self.assertEqual(template["capture_mode"], "interval")
        self.assertEqual(template["capture_point_time"], "00:00:00")
        self.assertEqual(template["capture_start_time"], "00:00:00")
        self.assertEqual(template["capture_end_time"], "00:01:00")
        self.assertEqual(template["stream_first_frame_timeout"], 120)
        self.assertEqual(template["update_url"], stream.DEFAULT_UPDATE_INFO)

    def test_fresh_load_config_keeps_connection_fields_blank(self):
        config_path = self.root / "data" / "config" / "stream_config.json"
        with mock.patch.object(stream, "CONFIG_PATH", config_path):
            config = stream.load_config()
        self.assertEqual(config["api_url"], "")
        self.assertEqual(config["api_key"], "")
        self.assertEqual(config["model"], "")
        self.assertEqual(config["ssh_api_path"], "")

    def test_config_save_failure_is_reported_to_ui_callback(self):
        notices = []
        with mock.patch.object(stream, "save_config", side_effect=OSError("disk full")):
            saved = stream.save_config_with_notice(
                self.config(),
                lambda title, message, level, log: notices.append((title, message, level, log)),
                "保存设置",
            )

        self.assertFalse(saved)
        self.assertEqual(notices[0][0], "配置保存失败")
        self.assertEqual(notices[0][2], "error")
        self.assertIn("disk full", notices[0][1])

    def test_update_system_local_json_download_and_sha(self):
        package = self.root / "Traffic Light_V2.2_Setup.exe"
        package.write_bytes(b"new-version")
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        update_json = self.root / "update.json"
        update_json.write_text(
            json.dumps(
                {
                    "latest_version": "2.2.0",
                    "version_code": 220,
                    "channel": "stable",
                    "minimum_supported_version": "2.0.0",
                    "force_update": False,
                    "package_type": "installer",
                    "download_url": package.name,
                    "file_size": f"{package.stat().st_size} B",
                    "sha256": digest,
                    "release_notes": ["测试更新"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        info = updater.check_for_update(str(update_json), stream.APP_VERSION, base_dirs=[self.root])
        self.assertTrue(info["has_update"])
        self.assertEqual(info["version_code"], 220)
        self.assertEqual(info["channel"], "stable")
        self.assertEqual(info["minimum_supported_version"], "2.0.0")
        self.assertFalse(info["force_update"])
        self.assertEqual(info["package_type"], "installer")
        self.assertEqual(info["file_size"], package.stat().st_size)
        self.assertIn("测试更新", info["release_notes"])
        downloaded = updater.download_update_file(info, self.root / "updates", base_dirs=[self.root])
        self.assertEqual(hashlib.sha256(downloaded.read_bytes()).hexdigest(), digest)

    def test_update_system_accepts_utf8_bom_update_json(self):
        update_json = self.root / "bom_update.json"
        update_json.write_text(
            "\ufeff"
            + json.dumps(
                {
                    "app_name": "Traffic Light",
                    "latest_version": "2.2.0",
                    "version_code": 220,
                    "download_url": "",
                    "sha256": "",
                    "file_size": 0,
                    "release_notes": ["BOM JSON"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        info = updater.check_for_update(str(update_json), stream.APP_VERSION, base_dirs=[self.root])
        self.assertTrue(info["has_update"])
        self.assertEqual(info["app_name"], "Traffic Light")
        self.assertIn("BOM JSON", info["release_notes"])

    def test_update_system_rejects_invalid_json_missing_file_bad_sha_and_cancel(self):
        bad_json = self.root / "bad.json"
        bad_json.write_text("{", encoding="utf-8")
        with self.assertRaises(updater.UpdateError):
            updater.check_for_update(str(bad_json), stream.APP_VERSION, base_dirs=[self.root])
        with self.assertRaises(updater.UpdateError):
            updater.check_for_update(str(self.root / "missing.json"), stream.APP_VERSION, base_dirs=[self.root])

        package = self.root / "pkg.exe"
        package.write_bytes(b"pkg")
        info = {
            "latest_version": "2.2.0",
            "download_url": str(package),
            "sha256": "0" * 64,
            "_source_location": str(self.root / "update.json"),
        }
        with self.assertRaises(updater.UpdateError):
            updater.download_update_file(info, self.root / "bad-sha", base_dirs=[self.root])
        size_info = {
            "latest_version": "2.2.0",
            "download_url": str(package),
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "file_size": package.stat().st_size + 1,
            "_source_location": str(self.root / "update.json"),
        }
        with self.assertRaises(updater.UpdateError):
            updater.download_update_file(size_info, self.root / "bad-size", base_dirs=[self.root])
        cancel = stream.threading.Event()
        cancel.set()
        info["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
        with self.assertRaises(updater.UpdateCancelled):
            updater.download_update_file(info, self.root / "cancel", base_dirs=[self.root], cancel_event=cancel)

    def test_report_records_frame_asset_and_missing_image_safely(self):
        config = self.config()
        engine = stream.AnalysisEngine(config, record_session=False)
        engine.current_input_source = {"type": "file", "value": str(self.root / "demo video.mp4")}
        frame = self.root / "frame_20260623_000001.jpg"
        stream.Image.new("RGB", (320, 180), color=(10, 120, 200)).save(frame, "JPEG")

        result = engine.record_result(frame, "第一条分析")
        asset = Path(result["frame_image_path"])
        self.assertTrue(asset.exists())
        text = engine.result_file.read_text(encoding="utf-8")
        self.assertIn("![抽帧图片]", text)
        self.assertIn("分析 0001", text)
        self.assertIn("视频时间轴 00:00:00", text)

        missing = engine.record_result(self.root / "missing_000002.jpg", "缺失图片分析")
        self.assertEqual(missing["frame_image_path"], "")
        text = engine.result_file.read_text(encoding="utf-8")
        self.assertIn("对应抽帧图片缺失", text)

    def test_load_config_clears_api_url_accidentally_saved_as_key(self):
        config_path = self.root / "data" / "config" / "stream_config.json"
        config_path.parent.mkdir(parents=True)
        api_url = "https://example.com/v1/chat/completions"
        config_path.write_text(
            json.dumps(
                {
                    "api_url": api_url,
                    "api_key": api_url,
                    "model": "",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(stream, "CONFIG_PATH", config_path):
            config = stream.load_config()
        self.assertEqual(config["api_url"], api_url)
        self.assertEqual(config["api_key"], "")

    def test_api_key_url_detection_preserves_normal_keys(self):
        api_url = "https://example.com/v1/chat/completions"
        self.assertTrue(stream.api_key_looks_like_url(api_url, api_url))
        self.assertEqual(stream.sanitize_api_key(api_url, api_url), "")
        self.assertFalse(stream.api_key_looks_like_url("sk-valid-secret", api_url))
        self.assertEqual(
            stream.sanitize_api_key("sk-valid-secret", api_url),
            "sk-valid-secret",
        )

    def test_runtime_rtsp_url_preserves_embedded_password_when_username_only(self):
        config = self.config()
        config.update(
            {
                "stream_url": "rtsp://admin:embedded-pass@example.com/live",
                "rtsp_username": "admin",
                "rtsp_password": "",
            }
        )
        runtime_url = stream.build_runtime_stream_url(config["stream_url"], config)
        self.assertIn("admin:embedded-pass@example.com", runtime_url)

    def test_runtime_rtsp_url_allows_explicit_password_override(self):
        config = self.config()
        config.update(
            {
                "stream_url": "rtsp://admin:embedded-pass@example.com/live",
                "rtsp_username": "operator",
                "rtsp_password": "explicit-pass",
            }
        )
        runtime_url = stream.build_runtime_stream_url(config["stream_url"], config)
        self.assertIn("operator:explicit-pass@example.com", runtime_url)
        self.assertNotIn("embedded-pass", runtime_url)

    def test_rtsp_input_options_avoid_unsupported_rw_timeout(self):
        engine = stream.AnalysisEngine(self.config(), record_session=False)
        options = engine.build_ffmpeg_input_options(
            "stream",
            "rtsp://admin:pass@example.com/live",
            low_latency=False,
            rtsp_transport="tcp",
        )
        self.assertNotIn("-rw_timeout", options)
        self.assertIn("-timeout", options)
        self.assertIn("-rtsp_transport", options)

    def test_stream_protocol_options_do_not_use_rw_timeout(self):
        engine = stream.AnalysisEngine(self.config(), record_session=False)
        urls = [
            "http://example.com/live.flv",
            "https://example.com/live.m3u8",
            "srt://example.com:9000",
            "udp://239.0.0.1:1234",
            "rtp://239.0.0.1:5004",
        ]
        for url in urls:
            with self.subTest(url=url):
                options = engine.build_ffmpeg_input_options("stream", url, low_latency=False)
                self.assertNotIn("-rw_timeout", options)
        self.assertIn("-reconnect", engine.build_ffmpeg_input_options("stream", urls[1], low_latency=False))
        self.assertIn("-connect_timeout", engine.build_ffmpeg_input_options("stream", urls[2], low_latency=False))
        self.assertNotIn("-timeout", engine.build_ffmpeg_input_options("stream", urls[4], low_latency=False))

    def test_rtsp_keyframe_rescue_adds_decoder_option(self):
        engine = stream.AnalysisEngine(self.config(), record_session=False)
        options = engine.build_ffmpeg_input_options(
            "stream",
            "rtsp://admin:pass@example.com/live",
            low_latency=False,
            rtsp_transport="tcp",
            keyframe_only=True,
        )
        self.assertIn("-skip_frame", options)
        self.assertIn("nokey", options)

    def test_realtime_interval_gate_drops_burst_frames(self):
        config = self.config()
        config.update({"capture_mode": "interval", "frame_interval": 5})
        engine = stream.AnalysisEngine(config, record_session=False)
        engine.realtime_source = True
        engine.ffmpeg_output_prefix = "frame_test"
        engine.stream_frame_interval = 5

        frame1 = self.root / "frame_test_000001.jpg"
        frame2 = self.root / "frame_test_000002.jpg"
        frame3 = self.root / "frame_test_000003.jpg"
        for frame in (frame1, frame2, frame3):
            frame.write_bytes(b"jpg")

        with mock.patch.object(stream.time, "monotonic", side_effect=[100.0, 102.0, 105.1]):
            self.assertTrue(engine.enqueue_image(str(frame1), quiet=True))
            self.assertFalse(engine.enqueue_image(str(frame2), quiet=True))
            self.assertTrue(engine.enqueue_image(str(frame3), quiet=True))

        self.assertTrue(frame1.exists())
        self.assertFalse(frame2.exists())
        self.assertTrue(frame3.exists())
        self.assertEqual(engine.task_queue.qsize(), 2)
        self.assertEqual(engine.stream_throttled_frames, 1)

    def test_rtsp_runtime_plan_prioritizes_stable_candidates(self):
        engine = stream.AnalysisEngine(self.config(), record_session=False)
        engine.prepare_rtsp_runtime_plan({"type": "stream", "value": "rtsp://admin:pass@example.com/live"})
        labels = [candidate["label"] for candidate in engine.rtsp_runtime_candidates]
        self.assertEqual(labels[0], "RTSP TCP稳定")
        self.assertIn("RTSP UDP稳定", labels)
        self.assertIn("RTSP HTTP隧道", labels)
        self.assertIn("RTSP TCP关键帧救援", labels)
        self.assertFalse(engine.stream_low_latency_override)
        self.assertEqual(engine.stream_transport_override, "tcp")

    def test_ignored_ffmpeg_exit_pid_does_not_schedule_restart(self):
        class DummyProcess:
            pid = 12345

        engine = stream.AnalysisEngine(self.config(), record_session=False)
        engine.ignored_ffmpeg_exit_pids.add(DummyProcess.pid)
        engine.handle_process_exit("FFmpeg", 1, DummyProcess())
        self.assertNotIn(DummyProcess.pid, engine.ignored_ffmpeg_exit_pids)

    def test_ffmpeg_hint_prioritizes_rtsp_option_compatibility(self):
        hint = stream.ffmpeg_stream_error_hint(
            "Option rw_timeout not found. Error opening input files: Option not found",
            "rtsp://admin:pass@example.com/live",
        )
        self.assertIn("兼容性问题", hint)
        self.assertNotIn("播放路径不存在", hint)

    def test_ffmpeg_hint_explains_rtsp_describe_500(self):
        hint = stream.ffmpeg_stream_error_hint(
            "method DESCRIBE failed: 500 (Internal Server Error) "
            "Error opening input: Server returned 5XX Server Error reply",
            "rtsp://admin:pass@example.com/live",
        )
        self.assertIn("DESCRIBE", hint)
        self.assertIn("账号密码", hint)

    def test_ffmpeg_hint_explains_hevc_decode_without_frame(self):
        hint = stream.ffmpeg_stream_error_hint(
            "Could not find ref with POC 80\nError constructing the frame RPS.",
            "rtsp://admin:pass@example.com/live",
        )
        self.assertIn("H.265", hint)
        self.assertIn("关键帧", hint)

    def test_hevc_decoder_noise_is_nonfatal(self):
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("Could not find ref with POC 80"))
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("Error constructing the frame RPS."))
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("The cu_qp_delta 35 is outside the valid range [-26, 25]."))

    def test_h264_waiting_for_keyframe_noise_is_nonfatal(self):
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("non-existing PPS 0 referenced"))
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("Error submitting packet to decoder: Invalid data found"))
        self.assertTrue(stream.is_ffmpeg_nonfatal_noise("Decode error rate 0.98 exceeds maximum 0.66"))

    def test_http_progressive_file_hint_is_clear(self):
        self.assertTrue(stream.is_progressive_http_video_url("https://example.com/video.mp4"))
        hint = stream.ffmpeg_stream_error_hint(
            "Invalid data found when processing input. Nothing was written into output file.",
            "https://example.com/video.mp4",
        )
        self.assertIn("HTTP", hint)
        self.assertIn("RTSP", hint)
        self.assertIn("HLS", hint)

    def test_capture_time_parser_accepts_seconds_minutes_and_hours(self):
        self.assertEqual(stream.parse_capture_time("12"), 12)
        self.assertEqual(stream.parse_capture_time("01:02"), 62)
        self.assertEqual(stream.parse_capture_time("01:02:03.5"), 3723.5)
        with self.assertRaises(ValueError):
            stream.parse_capture_time("01:99")

    def test_capture_plan_interval_keeps_legacy_behavior(self):
        config = self.config()
        config["frame_interval"] = 15
        plan = stream.build_capture_plan(config, "file")
        self.assertEqual(plan["mode"], "interval")
        self.assertEqual(plan["video_filter"], "fps=1/15")
        self.assertEqual(plan["input_options"], [])
        self.assertFalse(plan["finite"])

    def test_capture_plan_local_point_uses_seek_and_single_frame(self):
        config = self.config()
        config.update({"capture_mode": "point", "capture_point_time": "00:01:12.5"})
        plan = stream.build_capture_plan(config, "file")
        self.assertEqual(plan["input_options"], ["-ss", "72.5"])
        self.assertEqual(plan["output_options"], ["-frames:v", "1"])
        self.assertEqual(plan["video_filter"], "")
        self.assertTrue(plan["disable_local_readrate"])
        self.assertTrue(plan["finite"])

    def test_capture_plan_local_range_uses_seek_duration_and_interval(self):
        config = self.config()
        config.update(
            {
                "capture_mode": "range",
                "capture_start_time": "00:00:10",
                "capture_end_time": "00:00:40",
                "frame_interval": 6,
            }
        )
        plan = stream.build_capture_plan(config, "file")
        self.assertEqual(plan["input_options"], ["-ss", "10"])
        self.assertEqual(plan["output_options"], ["-t", "30"])
        self.assertEqual(plan["video_filter"], "fps=1/6")
        self.assertTrue(plan["finite"])

    def test_capture_plan_stream_point_is_relative_to_task_start(self):
        config = self.config()
        config.update({"capture_mode": "point", "capture_point_time": "00:00:08"})
        plan = stream.build_capture_plan(config, "stream")
        self.assertEqual(plan["input_options"], [])
        self.assertEqual(plan["output_options"], ["-frames:v", "1"])
        self.assertEqual(plan["video_filter"], "trim=start=8,setpts=PTS-STARTPTS")
        self.assertEqual(plan["first_frame_wait"], 8)
        self.assertTrue(plan["finite"])

    def test_capture_plan_stream_range_limits_input_until_end_time(self):
        config = self.config()
        config.update(
            {
                "capture_mode": "range",
                "capture_start_time": "00:00:05",
                "capture_end_time": "00:00:20",
                "frame_interval": 5,
            }
        )
        plan = stream.build_capture_plan(config, "stream")
        self.assertEqual(plan["input_options"], ["-t", "20"])
        self.assertEqual(
            plan["video_filter"],
            "trim=start=5:end=20,setpts=PTS-STARTPTS,fps=1/5",
        )
        self.assertEqual(plan["first_frame_wait"], 5)
        self.assertTrue(plan["finite"])

    def test_capture_plan_rejects_invalid_range(self):
        config = self.config()
        config.update(
            {
                "capture_mode": "range",
                "capture_start_time": "00:00:20",
                "capture_end_time": "00:00:10",
            }
        )
        with self.assertRaises(ValueError):
            stream.build_capture_plan(config, "file")

    def test_local_point_capture_removes_readrate_from_input_options(self):
        options = ["-readrate", "2.5", "-err_detect", "ignore_err"]
        self.assertEqual(
            stream.remove_ffmpeg_option_with_value(options, "-readrate"),
            ["-err_detect", "ignore_err"],
        )

    def test_config_snapshot_never_imports_or_exports_secrets(self):
        snapshot = self.root / "config.json"
        stream.export_config_snapshot(snapshot, self.config())
        raw = snapshot.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-secret-123456", raw)
        self.assertNotIn("camera-secret", raw)

        payload = json.loads(raw)
        payload["config"]["api_key"] = "malicious-imported-key"
        payload["config"]["rtsp_password"] = "malicious-imported-password"
        snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        current = self.config()
        imported = stream.import_config_snapshot(snapshot, current)
        self.assertEqual(imported["api_key"], current["api_key"])
        self.assertEqual(imported["rtsp_password"], current["rtsp_password"])

    def test_persistent_log_masks_credentials(self):
        with mock.patch.object(stream, "LOGS_DIR", self.root / "logs"):
            log_path = stream.write_persistent_log(
                "rtsp://admin:camera-secret@192.0.2.10/live "
                "Bearer abcdefghijklmnop sk-test-secret-123456"
            )
            self.assertIsNotNone(log_path)
            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn("camera-secret", text)
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertNotIn("sk-test-secret-123456", text)

    def test_persistent_log_batch_masks_credentials_and_uses_one_file(self):
        with mock.patch.object(stream, "LOGS_DIR", self.root / "logs"):
            paths = stream.write_persistent_logs(
                [
                    "first api_key=sk-test-secret-123456",
                    "second rtsp://admin:camera-secret@192.0.2.10/live",
                ]
            )
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text(encoding="utf-8")
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertNotIn("sk-test-secret-123456", text)
        self.assertNotIn("camera-secret", text)

    def test_engine_event_queue_overflow_preserves_log_on_disk(self):
        event_queue = queue.Queue(maxsize=1)
        event_queue.put(("stats", {}))
        with (
            mock.patch.object(stream, "LOGS_DIR", self.root / "logs"),
            mock.patch.object(stream, "write_persistent_log") as persistent_log,
        ):
            engine = stream.AnalysisEngine(
                self.config(),
                event_queue=event_queue,
                record_session=False,
            )
            emitted = engine.emit("log", {"text": "overflow log"})
        self.assertFalse(emitted)
        persistent_log.assert_called_once_with("overflow log")

    def test_realtime_terminal_cache_is_bounded(self):
        engine = stream.AnalysisEngine(self.config(), record_session=False)
        engine.realtime_source = True
        for index in range(stream.REALTIME_TERMINAL_CACHE_LIMIT + 5):
            engine.remember_terminal_path(f"frame_{index:06d}.jpg", index % 2 == 0)
        self.assertEqual(
            len(engine.terminal_path_order),
            stream.REALTIME_TERMINAL_CACHE_LIMIT,
        )
        self.assertEqual(
            len(engine.terminal_paths),
            stream.REALTIME_TERMINAL_CACHE_LIMIT,
        )
        self.assertNotIn("frame_000000.jpg", engine.terminal_paths)

    def test_session_manifest_records_terminal_state_without_secrets(self):
        config = self.config()
        with mock.patch.object(stream, "LOGS_DIR", self.root / "logs"):
            engine = stream.AnalysisEngine(config)
            engine.current_input_source = {
                "type": "stream",
                "value": config["stream_url"],
            }
            engine.update_session_manifest("completed", terminal=True)

        payload = json.loads(engine.session_manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["app_version"], "2.1.1")
        self.assertTrue(engine.session_terminal)
        raw = engine.session_manifest_file.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-secret-123456", raw)
        self.assertNotIn("camera-secret", raw)

    def test_support_bundle_contains_only_redacted_diagnostics(self):
        config = self.config()
        results_dir = Path(config["results_dir"])
        results_dir.mkdir(parents=True)
        (results_dir / "session_example.json").write_text(
            '{"api_key":"sk-test-secret-123456","rtsp_password":"camera-secret"}',
            encoding="utf-8",
        )
        destination = self.root / "support.zip"

        with mock.patch.object(stream, "LOGS_DIR", self.root / "logs"):
            stream.write_persistent_log("api_key=sk-test-secret-123456")
            stream.create_support_bundle(destination, config)

        with zipfile.ZipFile(destination) as archive:
            names = set(archive.namelist())
            self.assertIn("diagnostics.json", names)
            self.assertIn("latest_log_tail.txt", names)
            combined = "\n".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in names
            )
        self.assertNotIn("sk-test-secret-123456", combined)
        self.assertNotIn("camera-secret", combined)

    def test_retention_cleanup_only_removes_expired_runtime_files(self):
        logs = self.root / "logs"
        crashes = self.root / "crashes"
        logs.mkdir()
        crashes.mkdir()
        old_log = logs / "old.log"
        old_crash = crashes / "old.txt"
        recent_log = logs / "recent.log"
        for path in (old_log, old_crash, recent_log):
            path.write_text("test", encoding="utf-8")
        old_time = 1
        old_log.touch()
        old_crash.touch()
        import os

        os.utime(old_log, (old_time, old_time))
        os.utime(old_crash, (old_time, old_time))

        with (
            mock.patch.object(stream, "LOGS_DIR", logs),
            mock.patch.object(stream, "CRASH_DIR", crashes),
        ):
            removed = stream.cleanup_runtime_records(30)
        self.assertEqual(removed, 2)
        self.assertFalse(old_log.exists())
        self.assertFalse(old_crash.exists())
        self.assertTrue(recent_log.exists())

    def test_installer_and_uninstaller_are_side_by_side_safe(self):
        self.assertEqual(setup_installer.APP_VERSION, "2.1.1")
        self.assertIn("V2.1", setup_installer.APP_INSTALL_NAME)
        self.assertIn("_V2.1", setup_installer.EXE_NAME)
        self.assertEqual(
            setup_installer.APP_INSTALL_NAME,
            uninstall_helper.APP_INSTALL_NAME,
        )
        self.assertTrue(
            uninstall_helper.safe_install_dir(
                self.root / uninstall_helper.APP_INSTALL_NAME
            )
        )
        self.assertFalse(
            uninstall_helper.safe_install_dir(self.root / uninstall_helper.APP_NAME)
        )

    def test_portable_layout_and_resource_tool_priority(self):
        self.assertIn("V2.1_数据", stream.DATA_DIR.name)
        app_tools = self.root / "app_tools"
        resource_tools = self.root / "resource_tools"
        resource_tools.mkdir()
        bundled_ffmpeg = resource_tools / "ffmpeg.exe"
        bundled_ffmpeg.write_bytes(b"test")
        with (
            mock.patch.object(stream, "TOOLS_DIR", app_tools),
            mock.patch.object(stream, "RESOURCE_TOOLS_DIR", resource_tools),
        ):
            self.assertEqual(stream.find_tool("ffmpeg"), str(bundled_ffmpeg))

    def test_initial_window_geometry_fits_dpi_scaled_desktop(self):
        self.assertEqual(
            stream.calculate_initial_window_geometry(1920, 1080),
            (1280, 900, 320, 90),
        )
        self.assertEqual(
            stream.calculate_initial_window_geometry(1229, 819),
            (1149, 760, 40, 29),
        )
        width, height, x, y = stream.calculate_initial_window_geometry(1366, 768)
        self.assertGreaterEqual(width, stream.MIN_WINDOW_WIDTH)
        self.assertGreaterEqual(height, stream.MIN_WINDOW_HEIGHT)
        self.assertLessEqual(x + width, 1366)
        self.assertLessEqual(y + height, 768)

    def test_workflow_readiness_accepts_complete_single_page_configuration(self):
        video = self.root / "sample.mp4"
        video.write_bytes(b"video")
        readiness = stream.evaluate_workflow_readiness(
            {
                "source_type": "file",
                "video_file": str(video),
                "connection_mode": "public",
                "api_url": "https://example.com/v1/chat/completions",
                "api_key": "test-key",
                "model": "qwen3-vl-plus",
                "prompt": "分析画面",
                "selected_prompt_preset": "通用详细报告",
            }
        )
        self.assertTrue(readiness["ready"])
        self.assertTrue(readiness["source_ready"])
        self.assertTrue(readiness["server_ready"])
        self.assertTrue(readiness["prompt_ready"])

    def test_workflow_readiness_reports_missing_public_key(self):
        readiness = stream.evaluate_workflow_readiness(
            {
                "source_type": "stream",
                "stream_url": "rtsp://192.0.2.10/live",
                "connection_mode": "public",
                "api_url": "https://example.com/v1/chat/completions",
                "api_key": "",
                "model": "qwen3-vl-plus",
                "prompt": "分析画面",
            }
        )
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["server_ready"])
        self.assertIn("密钥", readiness["server_message"])

    def test_workflow_readiness_reports_incomplete_ssh_route(self):
        readiness = stream.evaluate_workflow_readiness(
            {
                "source_type": "stream",
                "stream_url": "https://example.com/live.m3u8",
                "connection_mode": "private_ssh",
                "api_url": "http://127.0.0.1:8080/v1/chat/completions",
                "api_key": "",
                "model": "qwen3-vl-plus",
                "ssh_host": "",
                "ssh_user": "",
                "ssh_remote_host": "",
                "prompt": "分析画面",
            }
        )
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["server_ready"])
        self.assertIn("SSH服务器", readiness["server_message"])

    def test_recent_session_index_skips_invalid_manifests(self):
        config = self.config()
        results_dir = Path(config["results_dir"])
        results_dir.mkdir(parents=True)
        (results_dir / "session_invalid.json").write_text("{", encoding="utf-8")
        (results_dir / "session_valid.json").write_text(
            json.dumps(
                {
                    "session_id": "valid",
                    "status": "completed",
                    "updated_at": "2026-06-13T10:00:00+08:00",
                    "source": {"type": "file", "value": "sample.mp4"},
                    "result_file": str(results_dir / "analysis.md"),
                    "stats": {"success": 3, "failed": 1},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sessions = stream.list_recent_sessions(config)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "valid")
        self.assertEqual(sessions[0]["success"], 3)
        self.assertEqual(sessions[0]["failed"], 1)

    def test_legacy_config_migration_preserves_supported_values(self):
        legacy = self.root / "stream_config.json"
        legacy.write_text(
            json.dumps(
                {
                    "api_url": "http://127.0.0.1:9000/v1/chat/completions",
                    "api_key": "legacy-secret",
                    "model": "legacy-vl",
                    "frame_interval": 18,
                    "unknown_field": "ignored",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        migrated = stream.import_legacy_config(legacy, self.config())
        self.assertEqual(migrated["api_key"], "legacy-secret")
        self.assertEqual(migrated["model"], "legacy-vl")
        self.assertEqual(migrated["frame_interval"], 18)
        self.assertNotIn("unknown_field", migrated)

    def test_installer_creates_v12_data_config_layout(self):
        payload = self.root / "payload"
        (payload / "tools").mkdir(parents=True)
        (payload / "assets").mkdir()
        required_files = {
            setup_installer.EXE_NAME: b"app",
            setup_installer.UNINSTALL_EXE_NAME: b"uninstaller",
            setup_installer.TEMPLATE_NAME: b'{"image_dir":"frames","results_dir":"results"}',
            setup_installer.README_NAME: b"readme",
            setup_installer.DOC_NAME: b"doc",
            setup_installer.CHANGELOG_NAME: b"changelog",
            "卸载本机安装.bat": b"bat",
            "tools/ffmpeg.exe": b"ffmpeg",
            "assets/app_icon.ico": b"ico",
            "assets/app_icon.png": b"png",
        }
        for relative, content in required_files.items():
            path = payload / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        target = self.root / setup_installer.APP_INSTALL_NAME

        def fake_resource_path(*parts):
            return payload.joinpath(*parts)

        with (
            mock.patch.object(setup_installer, "resource_path", fake_resource_path),
            mock.patch.object(setup_installer, "register_uninstall_entry"),
            mock.patch.object(setup_installer, "INSTALL_RECORD", self.root / "install.txt"),
        ):
            installed = setup_installer.install_to(
                target,
                create_desktop_shortcut=False,
                create_start_menu_shortcut=False,
                create_uninstall_shortcut=False,
            )
        self.assertEqual(installed, target / setup_installer.EXE_NAME)
        self.assertTrue(installed.is_file())
        self.assertTrue(
            (
                target
                / setup_installer.DATA_DIR_NAME
                / "config"
                / setup_installer.CONFIG_NAME
            ).is_file()
        )


class V10ImmutabilityTests(unittest.TestCase):
    def test_v10_files_match_recorded_baseline(self):
        version_dir = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (version_dir / "V1.0_BASELINE_SHA256.json").read_text(encoding="utf-8-sig")
        )
        source_root = Path(manifest["source_root"])

        import hashlib

        mismatches = []
        for item in manifest["files"]:
            path = source_root / item["relative_path"]
            if not path.is_file():
                mismatches.append(f"missing:{item['relative_path']}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            if digest != item["sha256"]:
                mismatches.append(f"changed:{item['relative_path']}")
        self.assertEqual(mismatches, [])


class V11ImmutabilityTests(unittest.TestCase):
    def test_v11_files_match_recorded_baseline(self):
        version_dir = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (version_dir / "V1.1_BASELINE_SHA256.json").read_text(encoding="utf-8-sig")
        )
        source_root = Path(manifest["source_root"])

        import hashlib

        mismatches = []
        for item in manifest["files"]:
            path = source_root / item["relative_path"]
            if not path.is_file():
                mismatches.append(f"missing:{item['relative_path']}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            if digest != item["sha256"]:
                mismatches.append(f"changed:{item['relative_path']}")
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
