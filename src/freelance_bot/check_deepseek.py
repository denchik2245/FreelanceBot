"""One paid JSON request; no VK messages or bot database changes."""

import asyncio
import json
import os

from dotenv import load_dotenv
from gigachat.models import Chat, JsonSchemaResponseFormat, Messages, MessagesRole

from freelance_bot.deepseek import DeepSeekClient


async def check() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Не задан DEEPSEEK_API_KEY в .env")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("DEEPSEEK_BASE_URL должен использовать HTTPS")
    model = os.getenv("DEEPSEEK_FILTER_MODEL", "deepseek-flash").strip()
    if not model:
        raise ValueError("DEEPSEEK_FILTER_MODEL пуст")
    async with DeepSeekClient(api_key=api_key, base_url=base_url, model=model) as client:
        response = await client.achat(Chat(
            model=model, temperature=0, max_tokens=64,
            response_format=JsonSchemaResponseFormat(schema={
                "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                "required": ["status"], "additionalProperties": False,
            }, strict=True),
            messages=[Messages(
                role=MessagesRole.USER,
                content='Проверь подключение. Верни JSON: {"status":"ok"}.',
            )],
        ))
    if (response.choices[0].finish_reason != "stop"
            or json.loads(response.choices[0].message.content) != {"status": "ok"}):
        raise ValueError("DeepSeek не вернул ожидаемый полный JSON")
    print("DeepSeek API подключён успешно; JSON проверен.")
    print(f"Модель ответа: {response.model}")


def main() -> None:
    load_dotenv()
    try:
        asyncio.run(check())
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
