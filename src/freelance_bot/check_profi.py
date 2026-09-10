import argparse
import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from freelance_bot.sources.profi import ProfiAuthenticationError, ProfiSource


async def check(
    *,
    cycles: int = 2,
    interval: float = 180,
    check_recovery: bool = False,
    check_reauth: bool = False,
) -> None:
    login = os.getenv("PROFI_LOGIN", "").strip()
    password = os.getenv("PROFI_PASSWORD", "").strip()
    if not login or not password:
        raise ValueError("Не заданы PROFI_LOGIN и PROFI_PASSWORD")

    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreelanceCategoryNotifier/1.0)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        source = ProfiSource(
            session,
            login,
            password,
            storage_state_path=Path(
                os.getenv("PROFI_STORAGE_STATE_PATH", "data/profi-storage-state.json")
            ),
        )
        try:
            projects = await source.fetch()
            print(f"Первичная загрузка: {len(projects)} карточек", flush=True)
            for cycle in range(1, cycles + 1):
                await asyncio.sleep(interval)
                projects = await source.fetch()
                print(f"Повторный цикл {cycle}/{cycles}: {len(projects)} карточек", flush=True)
            if check_reauth:
                # Invalidate only this command's isolated session, keeping the
                # source's auth flag to exercise detection of expired credentials.
                assert source._browser_context is not None
                assert source._browser_page is not None
                await source._browser_context.clear_cookies()
                await source._browser_page.goto(
                    "https://profi.ru/backoffice/", wait_until="domcontentloaded", timeout=15000
                )
                await source._browser_page.wait_for_function(
                    "document.cookie.includes('prfr_q_val=')", timeout=15000
                )
                projects = await source.fetch()
                print(
                    f"Восстановление после удаления cookies: {len(projects)} карточек", flush=True
                )
            if check_recovery:
                # Crash only the dedicated diagnostic browser. The next fetch
                # must create a new session and authenticate without stale state.
                assert source._browser is not None
                await source._browser.close()
                projects = await source.fetch()
                print(
                    f"Восстановление после закрытия браузера: {len(projects)} карточек", flush=True
                )
        finally:
            await source.close()

    print("Profi.ru подключён успешно.")
    print(f"Карточек в сохранённом фильтре: {len(projects)}")
    if projects:
        latest = max(
            projects,
            key=lambda project: project.published_at.timestamp() if project.published_at else 0,
        )
        print(f"Последняя карточка: {latest.title}")
        print(f"Ссылка: {latest.url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка Profi.ru без VK и AI")
    parser.add_argument(
        "--cycles", type=int, default=2, help="Число повторных циклов (по умолчанию 2)"
    )
    parser.add_argument("--interval", type=float, default=180, help="Пауза между циклами, секунды")
    parser.add_argument(
        "--check-recovery", action="store_true", help="Проверить перезапуск браузера"
    )
    parser.add_argument(
        "--check-reauth", action="store_true", help="Проверить вход после удаления cookies"
    )
    args = parser.parse_args()
    if args.cycles < 0 or args.interval < 60:
        parser.error("--cycles должен быть >= 0, --interval должен быть >= 60")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        asyncio.run(
            check(
                cycles=args.cycles,
                interval=args.interval,
                check_recovery=args.check_recovery,
                check_reauth=args.check_reauth,
            )
        )
    except (ProfiAuthenticationError, ValueError, RuntimeError, TimeoutError) as error:
        raise SystemExit(f"Проверка Profi.ru не пройдена: {error}") from None


if __name__ == "__main__":
    main()
