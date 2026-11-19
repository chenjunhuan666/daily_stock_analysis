"""Regression tests for preparing a closing report before its delivery time."""

from datetime import datetime, time, timedelta
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "market_review_delivery", ROOT / ".github/scripts/deliver_market_review.py",
)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class MarketReviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.report = Path(directory.name) / "market_review_20261119.md"
        self.report.write_text("# 大盘复盘\n收盘分析结果", encoding="utf-8")
        self.now = datetime(2026, 11, 19, 15, 12, tzinfo=delivery.BEIJING)
        self.notifier = Mock()
        self.notifier.send_with_results.return_value = SimpleNamespace(
            success=True, channel_results=[SimpleNamespace(channel="email", success=True)],
        )
        self.sent_at = []
        self.notifier.send_with_results.side_effect = self.record_send
        self.sleeps = []

    def record_send(self, *args, **kwargs):
        self.sent_at.append(self.now)
        return self.notifier.send_with_results.return_value

    def sleep(self, seconds):
        self.assertFalse(self.sent_at, "A report was sent before the delivery time")
        self.assertLessEqual(seconds, 60)
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def deliver(self, **kwargs):
        return delivery.deliver_report(
            self.report, self.notifier, not_before=time(16),
            now=lambda: self.now, sleep=self.sleep, **kwargs,
        )

    def test_prepared_report_waits_until_1600_then_sends_once(self):
        self.assertTrue(self.deliver())
        self.assertEqual(self.sent_at, [self.now.replace(hour=16, minute=0)])
        self.assertEqual(sum(self.sleeps), 48 * 60)
        self.notifier.send_with_results.assert_called_once_with(
            "# 大盘复盘\n收盘分析结果", email_send_to_all=True, route_type="report",
        )

    def test_late_analysis_sends_immediately(self):
        self.now = self.now.replace(hour=16, minute=7)
        self.assertTrue(self.deliver())
        self.assertEqual(self.sent_at, [self.now])
        self.assertFalse(self.sleeps)

    def test_forced_manual_run_sends_immediately(self):
        self.assertTrue(self.deliver(immediate=True))
        self.assertEqual(self.sent_at, [self.now])
        self.assertFalse(self.sleeps)

    def test_failed_email_is_a_failure_even_if_another_channel_succeeds(self):
        self.notifier.send_with_results.return_value = SimpleNamespace(
            success=True, channel_results=[
                SimpleNamespace(channel="email", success=False),
                SimpleNamespace(channel="feishu", success=True),
            ],
        )
        self.assertFalse(self.deliver(immediate=True))

    def test_missing_or_suppressed_notification_is_a_failure(self):
        self.notifier.send_with_results.return_value = SimpleNamespace(success=False, channel_results=[])
        self.assertFalse(self.deliver(immediate=True))

    def test_empty_or_old_report_is_never_sent(self):
        for name, content in (("market_review_20261119.md", "  "), ("market_review_20261118.md", "old")):
            with self.subTest(name=name):
                self.report = self.report.with_name(name)
                self.report.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.deliver()
        self.notifier.send_with_results.assert_not_called()

    def test_date_change_during_wait_prevents_stale_delivery(self):
        def cross_midnight(seconds):
            self.now = self.now.replace(day=20, hour=0, minute=0)

        with self.assertRaises(ValueError):
            delivery.deliver_report(
                self.report, self.notifier, not_before=time(16), now=lambda: self.now, sleep=cross_midnight,
            )
        self.notifier.send_with_results.assert_not_called()

    def test_cli_only_skips_missing_report_on_verified_nontrading_day(self):
        config = SimpleNamespace(trading_day_check_enabled=True, market_review_region="cn", log_dir="logs")
        calendar = SimpleNamespace(compute_effective_region=Mock(), get_open_markets_today=lambda: set())
        constructor = Mock(return_value=self.notifier)
        modules = {
            "src.config": SimpleNamespace(get_config=lambda: config),
            "src.logging_config": SimpleNamespace(setup_logging=Mock()),
            "src.notification": SimpleNamespace(NotificationService=constructor),
            "src.core.trading_calendar": calendar,
        }
        with patch.dict("sys.modules", modules), patch.object(delivery, "ROOT", self.report.parent), \
                patch.object(delivery, "datetime") as clock:
            clock.now.return_value = self.now
            for region, exit_code in (("", 0), ("cn", 1)):
                calendar.compute_effective_region.return_value = region
                with self.subTest(region=region):
                    self.assertEqual(delivery.main(["--not-before", "16:00"]), exit_code)
        self.notifier.send_with_results.assert_not_called()

    def test_workflow_prepares_without_notify_then_delivers_with_same_force_flag(self):
        workflow = yaml.load(
            (ROOT / ".github/workflows/00-daily-analysis.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        job = workflow["jobs"]["analyze"]
        self.assertEqual(job["env"]["TZ"], "Asia/Shanghai")
        self.assertIn("'90'", job["timeout-minutes"])
        run = next(step["run"] for step in job["steps"] if step.get("id") == "analysis")
        market_only = run.split('if [ "$MODE" = "market-only" ]; then', 1)[1].split("elif", 1)[0]
        self.assertEqual([line.strip() for line in market_only.strip().splitlines()], [
            "python main.py --market-review --no-notify $FORCE_RUN_ARG",
            "python .github/scripts/deliver_market_review.py --not-before 16:00 $FORCE_RUN_ARG",
        ])


if __name__ == "__main__":
    unittest.main()
