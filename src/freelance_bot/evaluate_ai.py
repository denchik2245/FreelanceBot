"""Explicit opt-in model evaluation; never sends VK messages or changes the bot database."""

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from freelance_bot.ai import GigaChatProjectAdvisor
from freelance_bot.config import Settings
from freelance_bot.models import Project


def metrics(rows: list[dict]) -> dict:
    labelled = [row for row in rows if row.get("expected") in {"accept", "reject", "unclear"}]
    completed = [row for row in labelled if "decision" in row]
    accepted = [row for row in completed if row["decision"] == "accept"]
    wanted = [row for row in completed if row["expected"] == "accept"]
    correct_accept = sum(row["expected"] == "accept" for row in accepted)
    return {
        "labelled": len(labelled), "completed": len(completed),
        "errors": len(labelled) - len(completed),
        "false_accepts": sum(row["expected"] != "accept" for row in accepted),
        "missed_accepts": sum(row["decision"] != "accept" for row in wanted),
        "precision": correct_accept / len(accepted) if accepted else None,
        "recall": correct_accept / len(wanted) if wanted else None,
    }


async def evaluate(args: argparse.Namespace) -> None:
    load_dotenv()
    settings = Settings.from_env()
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    rows = []
    advisor = GigaChatProjectAdvisor.from_paths(
        profile_path=settings.ai_profile_path,
        filter_prompt_path=settings.ai_filter_prompt_path,
        response_prompt_path=settings.ai_response_prompt_path,
        portfolio_path=settings.ai_portfolio_path,
        credentials=settings.gigachat_credentials, scope=settings.gigachat_scope,
        base_url=settings.gigachat_base_url, ca_bundle_file=settings.gigachat_ca_bundle_file,
        filter_model=settings.gigachat_filter_model,
        response_model=settings.gigachat_response_model, min_score=70,
        verify_accepted=settings.ai_verify_accepted,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    async with advisor:
        for case in cases[:args.limit]:
            project = Project(
                source="Evaluation", external_id=case["id"], title=case["title"],
                description=case["description"], price=case.get("price", ""),
                url="", category="",
            )
            row = dict(case)
            try:
                if not args.responses_only:
                    result = await advisor.assess(project)
                    row.update(decision=result.decision, evidence=result.evidence,
                               reason=result.reason, model=result.filter_model)
                if args.responses_only or (args.responses and case.get("expected") == "accept"):
                    row["response"] = await advisor.generate_response(project)
            except Exception as error:  # noqa: BLE001 - report a failed evaluation without secrets
                # Do not serialize request headers, credentials or transport exception bodies.
                row["error"] = type(error).__name__
            rows.append(row)
            args.output.write_text(json.dumps({
                "filter_revision": advisor.filter_revision,
                "metrics": None if args.responses_only else metrics(rows), "rows": rows,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            status = row.get("error", row.get("decision", "response generated"))
            print(f"{case['id']}: {status}", flush=True)
            if "error" in row:
                break  # Stop on infrastructure failure instead of spending the whole batch.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("eval/projects.json"))
    parser.add_argument("--output", type=Path, default=Path("data/eval-current.json"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--responses", action="store_true")
    parser.add_argument("--responses-only", action="store_true")
    parser.add_argument("--live", action="store_true", help="Use paid GigaChat API tokens")
    args = parser.parse_args()
    if not args.live:
        parser.error("Model calls require --live (uses GigaChat API tokens)")
    if args.limit < 1:
        parser.error("--limit must be positive")
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
