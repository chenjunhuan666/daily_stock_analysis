"""Skip a scheduled market review when today's closing report was already emailed."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BEIJING = timezone(timedelta(hours=8))
WORKFLOW = "00-daily-analysis.yml"
ANALYSIS_STEP = "执行股票分析"


def github_get(path):
    request = Request(
        os.environ["GITHUB_API_URL"] + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "market-review-schedule"},
    )
    # GitHub redirects job-log downloads to a signed URL on a different host.
    request.add_unredirected_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    with urlopen(request, timeout=20) as response:
        return response.read()


def get_json(path):
    return json.loads(github_get(path))


def already_emailed(log, report_name):
    return all(marker in log for marker in (
        "运行模式: market-only",
        report_name,
        "action=send_notification status=success",
        "邮件发送成功",
    ))


def skip_reason(now):
    cutoff = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < cutoff:
        return "当前还未到北京时间 16:00，跳过延迟到本日盘前或盘中的定时任务。"

    root = f"/repos/{os.environ['GITHUB_REPOSITORY']}/actions"
    cutoff_utc = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode({
        "branch": os.environ["GITHUB_REF_NAME"],
        "created": f">={cutoff_utc}",
        "status": "completed",
        "per_page": 100,
    })
    runs = get_json(f"{root}/workflows/{WORKFLOW}/runs?{query}")
    if runs["total_count"] > len(runs["workflow_runs"]):
        raise ValueError("Too many runs to check for a prior delivery")

    report_name = now.strftime("market_review_%Y%m%d.md")
    for run in runs["workflow_runs"]:
        # Include failed runs too: an upload or cleanup may fail after the email is accepted.
        jobs = get_json(f"{root}/runs/{run['id']}/jobs?filter=all&per_page=100")
        if jobs["total_count"] > len(jobs["jobs"]):
            raise ValueError("Too many jobs to check for a prior delivery")
        for job in jobs["jobs"]:
            if not any(
                step["name"] == ANALYSIS_STEP and step["started_at"] and step["conclusion"] != "skipped"
                for step in job["steps"]
            ):
                continue
            log = github_get(f"{root}/jobs/{job['id']}/logs").decode("utf-8", errors="replace")
            if already_emailed(log, report_name):
                return f"今天的大盘复盘已在运行 #{run['run_number']} 发出，跳过重复发送。"
    return None


def main():
    try:
        reason = skip_reason(datetime.now(BEIJING))
    except (URLError, TimeoutError, ValueError, KeyError) as error:
        # Report only the status or exception type. Signed log URLs are credentials.
        detail = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
        print(f"::error::无法核对大盘邮件的发送记录（{detail}）。")
        return 1

    print(reason or "今天尚无大盘复盘邮件发送记录，开始分析。")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"skip={'true' if reason else 'false'}", file=output)
    if reason:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            print(reason, file=summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
