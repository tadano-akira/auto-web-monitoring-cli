#!/usr/bin/env python3
"""Webページ更新監視ツール。

設定ファイル(YAML)に列挙した監視対象URLをヘッドレスChromeで定期取得し、
前回取得分とのハッシュ差分でページ更新を検知する。更新があり、かつ
キーワード条件に一致した場合のみメール通知する。同一日内の再通知は行わない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import yaml

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "monitoring.yaml"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"

logger = logging.getLogger("web_monitor")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)

    # Windowsのコンソール(既定cp932)で日本語ログが文字化けするのを防ぐ
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "monitor.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    console_handler = logging.StreamHandler(sys.stdout)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


@dataclass
class TimeWindow:
    start: dtime
    end: dtime

    def contains(self, now: datetime) -> bool:
        current = now.time()
        if self.start <= self.end:
            return self.start <= current <= self.end
        # 日をまたぐ時間帯 (例: 22:00-06:00) にも一応対応
        return current >= self.start or current <= self.end


@dataclass
class Monitor:
    id: int
    name: str
    url: str
    interval_minutes: int
    time_window: Optional[TimeWindow]
    keyword: str
    notify_to: str


def parse_time_window(raw: Optional[dict]) -> Optional[TimeWindow]:
    if not raw:
        return None
    start = datetime.strptime(raw["start"], "%H:%M").time()
    end = datetime.strptime(raw["end"], "%H:%M").time()
    return TimeWindow(start=start, end=end)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_monitors(config: dict) -> list[Monitor]:
    monitors = []
    for raw in config.get("monitors", []):
        monitors.append(
            Monitor(
                id=raw["id"],
                name=raw.get("name", f"監視No.{raw['id']}"),
                url=raw["url"],
                interval_minutes=raw.get("interval_minutes", 5),
                time_window=parse_time_window(raw.get("time_window")),
                keyword=raw["keyword"],
                notify_to=raw["notify_to"],
            )
        )
    return monitors


def state_path(monitor_id: int) -> Path:
    return STATE_DIR / f"monitor_{monitor_id}.json"


def load_state(monitor_id: int) -> dict:
    path = state_path(monitor_id)
    if not path.exists():
        return {"last_hash": None, "last_checked_at": None, "last_notified_date": None}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(monitor_id: int, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path = state_path(monitor_id)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def fetch_html(url: str, chrome_config: dict) -> str:
    binary = chrome_config.get("binary", "google-chrome")
    budget = chrome_config.get("virtual_time_budget_ms", 10000)
    timeout_sec = chrome_config.get("timeout_sec", 30)
    extra_args = chrome_config.get("extra_args", [])

    cmd = [
        binary,
        "--headless",
        "--disable-gpu",
        "--dump-dom",
        f"--virtual-time-budget={budget}",
        *extra_args,
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    if result.returncode != 0:
        raise RuntimeError(f"chrome headless failed (code={result.returncode}): {result.stderr.strip()[:500]}")
    return result.stdout


def hash_html(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def send_notification(smtp_config: dict, monitor: Monitor) -> None:
    password_env = smtp_config["password_env"]
    password = os.environ.get(password_env)
    if not password:
        logger.error("環境変数 %s が未設定のため通知メールを送信できません", password_env)
        return

    msg = EmailMessage()
    msg["Subject"] = f"[Web監視] 監視No.{monitor.id} {monitor.name} が更新されました"
    msg["From"] = smtp_config["from_addr"]
    msg["To"] = monitor.notify_to
    msg.set_content(
        "監視対象ページの更新を検知し、通知キーワードに一致しました。\n\n"
        f"監視No: {monitor.id}\n"
        f"監視名: {monitor.name}\n"
        f"URL: {monitor.url}\n"
        f"キーワード: {monitor.keyword}\n"
        f"検知日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    with smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_config["user"], password)
        smtp.send_message(msg)

    logger.info("監視No.%s: 通知メールを送信しました (宛先=%s)", monitor.id, monitor.notify_to)


def check_monitor(monitor: Monitor, chrome_config: dict, smtp_config: dict, dry_run: bool = False) -> None:
    state = load_state(monitor.id)
    now = datetime.now()

    try:
        html = fetch_html(monitor.url, chrome_config)
    except Exception as exc:
        logger.error("監視No.%s: ページ取得に失敗しました: %s", monitor.id, exc)
        return

    new_hash = hash_html(html)
    old_hash = state.get("last_hash")

    if old_hash is None:
        logger.info("監視No.%s: 初回取得のためベースラインを保存します", monitor.id)
        state["last_hash"] = new_hash
        state["last_checked_at"] = now.isoformat()
        save_state(monitor.id, state)
        return

    if new_hash == old_hash:
        logger.info("監視No.%s: 変更なし", monitor.id)
        state["last_checked_at"] = now.isoformat()
        save_state(monitor.id, state)
        return

    logger.info("監視No.%s: ページ内容の変更を検知しました", monitor.id)
    state["last_hash"] = new_hash
    state["last_checked_at"] = now.isoformat()

    if monitor.keyword in html:
        today_str = date.today().isoformat()
        if state.get("last_notified_date") == today_str:
            logger.info("監視No.%s: キーワード一致しましたが本日は通知済みのためスキップします", monitor.id)
        else:
            logger.info("監視No.%s: キーワード「%s」に一致したため通知します", monitor.id, monitor.keyword)
            if dry_run:
                logger.info("監視No.%s: --dry-run のためメール送信をスキップします", monitor.id)
            else:
                try:
                    send_notification(smtp_config, monitor)
                except Exception as exc:
                    logger.error("監視No.%s: 通知メール送信に失敗しました: %s", monitor.id, exc)
                    save_state(monitor.id, state)
                    return
            state["last_notified_date"] = today_str
    else:
        logger.info("監視No.%s: 変更はありましたがキーワードに一致しませんでした", monitor.id)

    save_state(monitor.id, state)


def is_due(monitor: Monitor, now: datetime) -> bool:
    if monitor.time_window and not monitor.time_window.contains(now):
        return False
    state = load_state(monitor.id)
    last_checked_at = state.get("last_checked_at")
    if not last_checked_at:
        return True
    last_dt = datetime.fromisoformat(last_checked_at)
    elapsed_min = (now - last_dt).total_seconds() / 60
    return elapsed_min >= monitor.interval_minutes


def run_once(monitors: list[Monitor], config: dict, dry_run: bool = False) -> None:
    now = datetime.now()
    for monitor in monitors:
        if is_due(monitor, now):
            check_monitor(monitor, config.get("chrome", {}), config["smtp"], dry_run=dry_run)


def run_loop(monitors: list[Monitor], config: dict, dry_run: bool = False) -> None:
    tick_sec = config.get("check_interval_sec", 60)
    logger.info("監視デーモンを開始します (監視対象数=%d, tick=%d秒)", len(monitors), tick_sec)
    while True:
        try:
            run_once(monitors, config, dry_run=dry_run)
        except Exception:
            logger.exception("監視サイクル中に予期しないエラーが発生しました")
        time.sleep(tick_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Webページ更新監視ツール")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="設定ファイルのパス")
    parser.add_argument("--once", action="store_true", help="デーモンループせず1回だけ全監視をチェックして終了する")
    parser.add_argument("--dry-run", action="store_true", help="メール送信を行わずログのみ出力する")
    args = parser.parse_args()

    setup_logging()

    if not args.config.exists():
        logger.error("設定ファイルが見つかりません: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)
    monitors = build_monitors(config)

    if not monitors:
        logger.error("設定ファイルに監視対象 (monitors) が1件もありません")
        sys.exit(1)

    if args.once:
        run_once(monitors, config, dry_run=args.dry_run)
    else:
        run_loop(monitors, config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
