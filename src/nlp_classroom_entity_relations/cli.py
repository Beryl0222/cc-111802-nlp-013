import argparse
import json
from pathlib import Path

from .repository import RecordRepository
from .workflow import TextWorkflow


def build_parser():
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    parser.add_argument(
        "action",
        choices=(
            "ingest",
            "list",
            "entities",
            "mentions",
            "relations",
            "cards",
            "search",
            "card",
            "mention",
            "assign",
            "merge",
            "split",
            "confirm",
            "decisions",
            "events",
        ),
    )
    parser.add_argument("values", nargs="*", default=[])
    parser.add_argument("--state", default="var/records.json")
    parser.add_argument("--record", default=None)
    parser.add_argument("--entity", default=None)
    parser.add_argument("--predicate", default=None)
    parser.add_argument(
        "--review-status",
        default=None,
        choices=("confirmed", "unreviewed", "needs_reconfirmation"),
    )
    parser.add_argument("--actor", default="teacher")
    parser.add_argument("--target", default=None)
    parser.add_argument("--reject", action="store_true")
    return parser


def run(parser, args):
    workflow = TextWorkflow(RecordRepository(args.state))
    if args.action == "ingest":
        path = args.values[0] if args.values else "fixtures/records.json"
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        return workflow.ingest(values)
    if args.action == "list":
        return workflow.list_records()
    if args.action == "entities":
        return workflow.list_entities()
    if args.action == "mentions":
        return workflow.list_mentions(record_id=args.record)
    if args.action == "relations":
        return workflow.list_relations(record_id=args.record)
    if args.action == "cards":
        return workflow.list_cards(record_id=args.record)
    if args.action == "search":
        return workflow.search(
            entity=args.entity,
            predicate=args.predicate,
            record_id=args.record,
            review_status=args.review_status,
        )
    if args.action == "card":
        if not args.values:
            raise SystemExit("card 需要关系身份")
        return workflow.card(args.values[0])
    if args.action == "mention":
        if not args.values:
            raise SystemExit("mention 需要提及身份")
        return workflow.mention(args.values[0])
    if args.action == "assign":
        if len(args.values) != 2:
            raise SystemExit("assign 需要提及身份和实体身份")
        return workflow.assign_mention(args.values[0], args.values[1], actor=args.actor)
    if args.action == "merge":
        if len(args.values) < 2:
            raise SystemExit("merge 需要至少两个实体身份")
        return workflow.merge_entities(
            args.values, target_entity_id=args.target, actor=args.actor
        )
    if args.action == "split":
        if len(args.values) < 2:
            raise SystemExit("split 需要合并实体身份和至少一个待拆分实体身份")
        return workflow.split_entities(
            args.values[0],
            args.values[1:],
            target_entity_id=args.target,
            actor=args.actor,
        )
    if args.action == "confirm":
        if not args.values:
            raise SystemExit("confirm 需要关系身份")
        return workflow.confirm_relation(
            args.values[0], actor=args.actor, confirmed=not args.reject
        )
    if args.action == "decisions":
        return workflow.list_decisions()
    if args.action == "events":
        return workflow.list_events()
    raise SystemExit(f"未知操作：{args.action}")


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(parser, args)
    except (ValueError, TypeError, OSError) as error:
        parser.exit(2, f"错误：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
