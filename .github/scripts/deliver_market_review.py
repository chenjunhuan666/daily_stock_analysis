"""Send a prepared closing report at or after the requested Beijing time."""

import argparse
from datetime import datetime, time as clock_time, timedelta, timezone
import logging
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
BEIJING = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def deliver_report(report_path, notifier, *, not_before, immediate=False, now=None, sleep=time.sleep):
    """Keep the generated report, wait for delivery time, and check the send result."""
    now = now or (lambda: datetime.now(BEIJING))
    started = now()
    expected_name = started.strftime("market_review_%Y%m%d.md")
    if report_path.name != expected_name:
        raise ValueError("Refusing to send a report from a different Beijing date")
    content = report_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("The closing report is empty")

    delivery_time = datetime.combine(started.date(), not_before, tzinfo=BEIJING)
    logger.info("大盘报告已准备：%s；北京时间发送下限 %s", report_path.name, delivery_time.isoformat())
    if not immediate:
        while (remaining := (delivery_time - now()).total_seconds()) > 0:
            sleep(min(remaining, 60))
    if now().date() != started.date():
        raise ValueError("The prepared report expired before delivery")

    result = notifier.send_with_results(content, email_send_to_all=True, route_type="report")
    # A successful future channel must not hide a failed email delivery.
    email_failed = any(item.channel == "email" and not item.success for item in result.channel_results)
    if not result.success or email_failed:
        logger.error("action=send_notification status=failed report=%s", report_path.name)
        return False
    logger.info("action=send_notification status=success report=%s", report_path.name)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--not-before", required=True, type=clock_time.fromisoformat)
    parser.add_argument("--force-run", action="store_true", help="Skip trading-day checks and send immediately")
    args = parser.parse_args(argv)
    if args.not_before.tzinfo is not None:
        parser.error("--not-before must be a Beijing local time without a timezone suffix")

    sys.path.insert(0, str(ROOT))
    from src.config import get_config
    from src.logging_config import setup_logging
    from src.notification import NotificationService

    config = get_config()
    setup_logging(log_prefix="stock_analysis", log_dir=config.log_dir)
    if not args.force_run and config.trading_day_check_enabled:
        from src.core.trading_calendar import compute_effective_region, get_open_markets_today

        if compute_effective_region(config.market_review_region or "cn", get_open_markets_today()) == "":
            logger.info("今日大盘复盘相关市场均为非交易日，跳过推送。")
            return 0

    report_path = ROOT / "reports" / datetime.now(BEIJING).strftime("market_review_%Y%m%d.md")
    try:
        success = deliver_report(
            report_path, NotificationService(), not_before=args.not_before, immediate=args.force_run,
        )
    except (OSError, ValueError) as error:
        logger.error("大盘推送未完成（%s），请检查本次报告及发送日志。", type(error).__name__)
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
