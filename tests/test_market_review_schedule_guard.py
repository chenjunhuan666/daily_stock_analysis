"""Regression coverage for delayed market-review schedules and delivery checks."""

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/check_market_review_schedule.py"
SPEC = importlib.util.spec_from_file_location("market_review_schedule_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class MarketReviewScheduleGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 16, 13, tzinfo=guard.BEIJING)
        self.environment = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "GITHUB_EVENT_NAME": "schedule",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_workflow_uses_utc_schedules_for_beijing_afternoon(self):
        path = SCRIPT.parents[1] / "workflows/00-daily-analysis.yml"
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["schedule"], [
            {"cron": "30 7 * * 1-5"},
            {"cron": "13 8 * * 1-5"},
        ])
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"], "market-only")
        monday = datetime(2026, 11, 23, tzinfo=timezone.utc)
        for offset in range(5):
            for entry, expected in zip(workflow["on"]["schedule"], ((15, 30), (16, 13))):
                minute, hour, _, _, weekdays = entry["cron"].split()
                scheduled = (monday + timedelta(days=offset)).replace(hour=int(hour), minute=int(minute))
                local = scheduled.astimezone(guard.BEIJING)
                with self.subTest(day=offset, cron=entry["cron"]):
                    self.assertEqual(weekdays, "1-5")
                    self.assertEqual((local.hour, local.minute), expected)
                    self.assertEqual(local.date(), scheduled.date())

    def test_delayed_early_morning_schedule_is_an_error(self):
        with patch.object(guard, "get_json") as github:
            for stamp in (
                "2026-11-19T01:08:00+08:00", "2026-11-19T01:13:00+08:00",
                "2026-11-19T01:31:00+08:00", "2026-11-21T00:17:45+08:00",
                "2026-11-21T00:56:59+08:00", "2026-11-24T03:23:59+08:00",
                "2026-11-24T04:11:01+08:00",
            ):
                with self.subTest(stamp=stamp), self.assertRaises(guard.ScheduleTimingError):
                    guard.skip_reason(datetime.fromisoformat(stamp))
            github.assert_not_called()

    def test_early_schedule_does_not_write_successful_skip(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict(os.environ, GITHUB_OUTPUT=str(output)), patch.object(guard, "datetime") as clock:
                clock.now.return_value = self.now.replace(hour=1)
                self.assertEqual(guard.main(), 1)
            self.assertFalse(output.exists())

    def test_manual_early_review_can_run(self):
        with patch.object(guard, "get_json", return_value={"total_count": 0, "workflow_runs": []}):
            self.assertIsNone(guard.skip_reason(self.now.replace(hour=10), require_closing_time=False))

    def test_post_close_run_starts_analysis_and_checks_from_market_close(self):
        with patch.object(guard, "get_json", return_value={"total_count": 0, "workflow_runs": []}) as github:
            self.assertIsNone(guard.skip_reason(self.now.replace(hour=15, minute=30)))
            self.assertIn("2026-11-19T07%3A00%3A00Z", github.call_args.args[0])

    def test_only_a_delivered_current_market_report_skips(self):
        delivered = (
            "运行模式: market-only\nmarket_review_20261119.md\n"
            "action=send_notification status=success\n邮件发送成功"
        )
        runs = {"total_count": 1, "workflow_runs": [{"id": 10, "run_number": 5}]}
        jobs = {"total_count": 1, "jobs": [{"id": 20, "steps": [{
            "name": guard.ANALYSIS_STEP, "started_at": "2026-11-19T08:00:00Z", "conclusion": "success",
        }]}]}
        for log, skip in (
            (delivered, True),
            (delivered.replace("20261119", "20261118"), False),
            (delivered.replace("邮件发送成功", "发送失败"), False),
            (delivered.replace("status=success", "status=failed"), False),
            (delivered.replace("market-only", "stocks-only"), False),
        ):
            with self.subTest(log=log), patch.object(guard, "get_json", side_effect=[runs, jobs]), \
                    patch.object(guard, "github_get", return_value=log.encode("utf-8")):
                self.assertEqual(bool(guard.skip_reason(self.now)), skip)

    def test_api_failure_does_not_claim_success(self):
        with patch.object(guard, "skip_reason", side_effect=guard.URLError("unavailable")):
            self.assertEqual(guard.main(), 1)


if __name__ == "__main__":
    unittest.main()
