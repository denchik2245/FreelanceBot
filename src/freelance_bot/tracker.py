"""Command line access to the response tracker for the weekly review.

    python -m freelance_bot.tracker export [--days 10]
    python -m freelance_bot.tracker apply FILE|-
    python -m freelance_bot.tracker digest [--send]

On the server: ``docker compose exec -T bot python -m freelance_bot.tracker …``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from freelance_bot.response_log import format_digest
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import DISPLAY_TZ

DIGEST_STATE = "tracker:last_digest_week"


def digest_week(moment: datetime) -> str:
    year, week, _ = moment.astimezone(DISPLAY_TZ).isocalendar()
    return f"{year}-W{week:02d}"


def export_entries(store: ProjectStore, *, days: int, now: datetime | None = None) -> dict:
    moment = now or datetime.now(UTC)
    recent = store.responses.entries(since=moment - timedelta(days=days), include_legacy=True)
    keys = {entry.project_key for entry in recent}
    open_older = [
        entry for entry in store.responses.open_entries(now=moment) if entry.project_key not in keys
    ]
    last = store.responses.last_review()
    return {
        "generated_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "last_review": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
        "entries": [entry.as_dict() for entry in [*recent, *open_older]],
    }


def apply_file(store: ProjectStore, payload: Any) -> dict[str, int]:
    items = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Ожидается список записей или объект с полем entries")
    result = store.responses.apply(
        items, load_project=store.get_project, load_assessment=store.get_ai_assessment
    )
    store.responses.mark_reviewed()
    return result


async def send_digest(text: str) -> None:
    import aiohttp

    from freelance_bot.vk import VkBot

    token = os.environ["VK_GROUP_TOKEN"]
    user_id = int(os.environ["VK_USER_ID"])
    version = os.getenv("VK_API_VERSION", "5.199")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        await VkBot(session, token, user_id, version).send_text(text)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="tracker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="выгрузить отклики для сверки с биржами")
    export.add_argument("--days", type=int, default=10)
    apply = sub.add_parser("apply", help="записать итоги сверки из JSON")
    apply.add_argument("file", help="путь к JSON или - для stdin")
    digest = sub.add_parser("digest", help="недельная сводка")
    digest.add_argument("--days", type=int, default=7)
    digest.add_argument("--send", action="store_true", help="отправить сводку в VK")
    args = parser.parse_args(argv)

    store = ProjectStore(Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")))
    try:
        if args.command == "export":
            json.dump(export_entries(store, days=args.days), sys.stdout, ensure_ascii=False,
                      indent=1)
            sys.stdout.write("\n")
        elif args.command == "apply":
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text("utf-8")
            result = apply_file(store, json.loads(raw))
            print(f"Обновлено: {result['updated']}, добавлено: {result['created']}")
        else:
            now = datetime.now(UTC)
            text = format_digest(store.responses, now=now, display_tz=DISPLAY_TZ, days=args.days)
            print(text)
            if args.send:
                asyncio.run(send_digest(text))
                store.set_state(DIGEST_STATE, digest_week(now))
                print("\nОтправлено в VK.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
