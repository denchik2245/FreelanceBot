from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from freelance_bot.fl_mail import GmailFlMailbox


async def _check() -> None:
    address = os.getenv("FL_GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("FL_GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not app_password:
        raise RuntimeError(
            "Заполните FL_GMAIL_ADDRESS и FL_GMAIL_APP_PASSWORD в .env"
        )
    mailbox = GmailFlMailbox(address, app_password)
    batch = await mailbox.fetch(last_uid=None, uid_validity=None)
    print(
        "Gmail доступен: INBOX открыт только для чтения, "
        f"UIDVALIDITY={batch.uid_validity}, последний UID={batch.last_uid}"
    )


def main() -> None:
    load_dotenv()
    asyncio.run(_check())


if __name__ == "__main__":
    main()
