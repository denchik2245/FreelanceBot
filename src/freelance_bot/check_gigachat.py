import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole


async def check() -> None:
    credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
    if not credentials:
        raise ValueError("Не задан GIGACHAT_CREDENTIALS")
    model = os.getenv("GIGACHAT_FILTER_MODEL", "GigaChat-2").strip()
    if not model:
        raise ValueError("GIGACHAT_FILTER_MODEL пуст")
    ca_bundle = os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
    options: dict[str, object] = {
        "credentials": credentials,
        "scope": os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        "base_url": os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1"),
        "model": model,
        "max_retries": 3,
    }
    if ca_bundle:
        certificate = Path(ca_bundle)
        if not certificate.is_file():
            raise FileNotFoundError(f"Не найден сертификат GigaChat: {certificate}")
        options["ca_bundle_file"] = str(certificate)

    async with GigaChat(**options) as client:
        response = await client.achat(
            Chat(
                model=model,
                messages=[
                    Messages(
                        role=MessagesRole.USER,
                        content="Ответь строго одной строкой: GigaChat API подключён успешно.",
                    )
                ],
            )
        )
        print(response.choices[0].message.content)
        print(f"Модель ответа: {response.model or model}")


def main() -> None:
    load_dotenv()
    asyncio.run(check())


if __name__ == "__main__":
    main()
