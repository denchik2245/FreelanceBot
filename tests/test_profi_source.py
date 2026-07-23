from datetime import datetime, timezone

from freelance_bot.sources.profi import parse_projects


def test_parse_projects() -> None:
    payload = {
        "data": {
            "boSearchBoardItems": {
                "items": [
                    {
                        "id": "987654",
                        "type": "SNIPPET",
                        "title": "Создать лендинг на Tilda",
                        "description": "Нужен дизайн и сборка",
                        "lastUpdateDate": "2026-07-17T15:20:00Z",
                        "price": {"prefix": "до", "value": "15 000", "suffix": "₽"},
                    },
                    {"id": "block", "type": "PREMIUM_BLOCK"},
                ]
            }
        }
    }

    projects = parse_projects(payload)

    assert len(projects) == 1
    assert projects[0].source == "Profi.ru"
    assert projects[0].title == "Создать лендинг на Tilda"
    assert projects[0].price == "до 15 000 ₽"
    assert projects[0].url == "https://profi.ru/backoffice/n.php?o=987654"
    assert projects[0].published_at == datetime(2026, 7, 17, 15, 20, tzinfo=timezone.utc)
