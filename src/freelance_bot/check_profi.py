import asyncio
import os

import aiohttp
from dotenv import load_dotenv

from freelance_bot.sources.profi import ProfiAuthenticationError, ProfiSource


async def check() -> None:
    login = os.getenv("PROFI_LOGIN", "").strip()
    password = os.getenv("PROFI_PASSWORD", "").strip()
    if not login or not password:
        raise ValueError("Не заданы PROFI_LOGIN и PROFI_PASSWORD")

    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreelanceCategoryNotifier/1.0)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        projects = await ProfiSource(session, login, password).fetch()

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
    load_dotenv()
    try:
        asyncio.run(check())
    except ProfiAuthenticationError as error:
        raise SystemExit(f"Проверка Profi.ru не пройдена: {error}") from None


if __name__ == "__main__":
    main()
