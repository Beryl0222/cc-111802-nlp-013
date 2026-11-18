import json

from .knowledge import ACTIVE
from .records import TextRecord
from .text import annotate, normalize_text


class TextWorkflow:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------- ingestion
    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = [self._prepare(value) for value in values]
        state = self.repository.read_state()
        current = {record["record_id"]: record for record in state["records"]}
        stored = 0
        for record in incoming:
            previous = current.get(record["record_id"])
            if previous is None or record["revision"] >= previous["revision"]:
                if previous is not None and record["revision"] == previous["revision"]:
                    if _content(previous) == _content(record):
                        continue
                    raise ValueError(
                        f"记录 {record['record_id']} 重复修订 {record['revision']} "
                        "但内容不同"
                    )
                current[record["record_id"]] = record
                stored += 1
        state["records"] = sorted(current.values(), key=lambda row: row["record_id"])
        # Validate derived state before writing so a partially valid batch can
        # never leave a state file that the rebuild cannot read.
        self._snapshot(state)
        self._append_event(
            state,
            "ingest",
            "system",
            {
                "received": len(incoming),
                "stored": stored,
                "record_ids": sorted({record["record_id"] for record in incoming}),
            },
        )
        self.repository.replace_state(state)
        return {"received": len(incoming), "stored": len(state["records"])}

    @staticmethod
    def _prepare(value):
        record = TextRecord.from_dict(value).to_dict()
        normalized = dict(record)
        normalized["text"] = normalize_text(record["text"])
        normalized["mentions"] = _renormalize_mentions(record["text"], record["mentions"])
        normalized["analysis"] = _analysis(normalized)
        return normalized

    # ---------------------------------------------------------------- queries
    def list_records(self, source=None):
        records = self.repository.read_state()["records"]
        if source:
            records = [record for record in records if record["source"] == source]
        return records

    def snapshot(self):
        """Return derived entities, mentions, relations and cards."""
        return self._snapshot(self.repository.read_state())

    def _snapshot(self, state):
        from .knowledge import rebuild

        return rebuild(state["records"], state["decisions"])

    def list_entities(self):
        return self.snapshot()["entities"]

    def list_mentions(self, record_id=None):
        rows = self.snapshot()["mentions"]
        if record_id:
            rows = [row for row in rows if row["record_id"] == record_id]
        return rows

    def list_relations(self, record_id=None):
        rows = self.snapshot()["relations"]
        if record_id:
            rows = [row for row in rows if row["record_id"] == record_id]
        return rows

    def list_cards(self, record_id=None):
        rows = [dict(card) for card in self.snapshot()["cards"]]
        if record_id:
            rows = [row for row in rows if row["record_id"] == record_id]
        return rows

    def search(self, entity=None, predicate=None, record_id=None, review_status=None):
        """Query relation cards by endpoint, predicate, source or review state."""
        rows = self.list_cards(record_id=record_id)
        if entity:
            needle = normalize_text(entity)
            rows = [
                row
                for row in rows
                if needle
                in {
                    normalize_text(value)
                    for endpoint in (row["subject"], row["object"])
                    for value in (
                        endpoint["entity_id"] or "",
                        endpoint["label"] or "",
                        *endpoint.get("candidate_labels", []),
                    )
                    if value
                }
            ]
        if predicate:
            rows = [row for row in rows if row["predicate"] == predicate]
        if review_status:
            rows = [row for row in rows if row["review_status"] == review_status]
        return rows

    def card(self, relation_id):
        """Return one card with the sourced sentences a teacher should re-read."""
        card = next(
            (row for row in self.list_cards() if row["relation_id"] == relation_id),
            None,
        )
        if card is None:
            return None
        record = next(
            (
                row
                for row in self.list_records()
                if row["record_id"] == card["record_id"]
            ),
            None,
        )
        detail = dict(card)
        detail["source_text"] = record["text"] if record else None
        detail["record_observed_at"] = record["observed_at"] if record else None
        detail["context"] = record.get("context", "") if record else None
        detail["sentences"] = []
        if record is not None:
            cited = _cited_indices(card)
            evidence = card["evidence"]
            for sentence in record.get("analysis", {}).get("sentences", []):
                overlaps = (
                    sentence["start"] < evidence["end"]
                    and evidence["start"] < sentence["end"]
                )
                if overlaps or sentence["index"] in cited:
                    detail["sentences"].append(
                        {
                            "index": sentence["index"],
                            "start": sentence["start"],
                            "end": sentence["end"],
                            "text": sentence["text"],
                            "contains_evidence": overlaps,
                        }
                    )
        return detail

    def mention(self, mention_id):
        mention = self._mention_by_id(mention_id)
        if mention is None:
            return None
        cards = [
            card
            for card in self.list_cards()
            if mention_id in card["mention_ids"]
        ]
        return {"mention": mention, "cards": cards}

    # ------------------------------------------------------- human decisions
    def assign_mention(self, mention_id, entity_id, actor="teacher", at=None):
        """Resolve a pronoun or ambiguous mention to one entity.

        Any derived card whose evidence uses this mention is rebuilt from the
        new referent on the next read; no card is patched in place.
        """
        state = self.repository.read_state()
        mention = next(
            (
                row
                for row in self._snapshot(state)["mentions"]
                if row["mention_id"] == mention_id
            ),
            None,
        )
        if mention is None:
            raise ValueError(f"提及不存在：{mention_id}")
        active = {
            row["entity_id"]
            for row in self._snapshot(state)["entities"]
            if row["status"] == ACTIVE
        }
        if entity_id not in active:
            raise ValueError(f"实体不存在或已被合并：{entity_id}")
        state["decisions"].append(
            {
                "decision_id": _next_id(state["decisions"], "decision"),
                "action": "assign_mention",
                "record_id": mention["record_id"],
                "record_revision": mention["record_revision"],
                "mention_index": mention["annotation_index"],
                "mention_key": mention["mention_key"],
                "surface": mention["surface"],
                "entity_id": entity_id,
                "actor": actor,
                "at": _timestamp(at),
            }
        )
        self._append_event(
            state,
            "assign_mention",
            actor,
            {
                "mention_key": mention["mention_key"],
                "surface": mention["surface"],
                "entity_id": entity_id,
            },
        )
        self._commit(state)
        return self.mention(mention["mention_id"])

    def merge_entities(self, entity_ids, target_entity_id=None, actor="teacher", at=None):
        state = self.repository.read_state()
        snapshot = self._snapshot(state)
        active = {
            row["entity_id"]
            for row in snapshot["entities"]
            if row["status"] == ACTIVE
        }
        entity_ids = list(dict.fromkeys(entity_ids))
        if len(entity_ids) < 2:
            raise ValueError("合并需要至少两个实体")
        missing = [entity_id for entity_id in entity_ids if entity_id not in active]
        if missing:
            raise ValueError(f"实体不存在或已被合并：{', '.join(missing)}")
        target = target_entity_id or entity_ids[0]
        if target not in entity_ids:
            raise ValueError("合并目标必须包含在合并实体内")
        state["decisions"].append(
            {
                "decision_id": _next_id(state["decisions"], "decision"),
                "action": "merge",
                "entity_ids": entity_ids,
                "target_entity_id": target,
                "actor": actor,
                "at": _timestamp(at),
            }
        )
        self._append_event(
            state,
            "merge_entities",
            actor,
            {"entity_ids": entity_ids, "target_entity_id": target},
        )
        self._commit(state)
        return self._snapshot(state)

    def split_entities(
        self, entity_id, entity_ids, target_entity_id=None, actor="teacher", at=None
    ):
        """Undo a wrong merge.

        Only relations whose own mention is anchored to a split base identity
        become visible again under that person.  Relations of the remaining
        person never move, because identity is derived from the mention rather
        than inherited from the merged group.  The merged target itself may be
        split out; in that case ``target_entity_id`` (or the first remaining
        member) becomes the new merged identity.
        """
        state = self.repository.read_state()
        snapshot = self._snapshot(state)
        active = {
            row["entity_id"]
            for row in snapshot["entities"]
            if row["status"] == ACTIVE
        }
        if entity_id not in active:
            raise ValueError(f"实体不存在或已被合并：{entity_id}")
        split_ids = list(dict.fromkeys(entity_ids))
        if not split_ids:
            raise ValueError("拆分需要至少一个实体")
        merged_rows = {
            row["entity_id"]: row
            for row in snapshot["entities"]
            if row["status"] == "merged"
        }
        for split_id in split_ids:
            if split_id == entity_id:
                # The canonical target may itself be the mistaken identity;
                # remaining members then adopt a new merged target.
                continue
            row = merged_rows.get(split_id)
            if row is None or row.get("merged_into") != entity_id:
                raise ValueError(f"实体 {split_id} 不属于合并实体 {entity_id}")
        current = next(
            row for row in snapshot["entities"] if row["entity_id"] == entity_id
        )
        remaining = [
            member
            for member in current.get("member_entity_ids", [entity_id])
            if member not in split_ids
        ]
        if not remaining:
            raise ValueError("拆分后至少要保留一个实体作为合并身份")
        if target_entity_id is not None:
            if target_entity_id not in remaining:
                raise ValueError("拆分后的合并目标必须是保留下来的实体")
        elif entity_id in split_ids:
            target_entity_id = remaining[0]
        state["decisions"].append(
            {
                "decision_id": _next_id(state["decisions"], "decision"),
                "action": "split",
                "entity_id": entity_id,
                "entity_ids": split_ids,
                "target_entity_id": target_entity_id,
                "actor": actor,
                "at": _timestamp(at),
            }
        )
        self._append_event(
            state,
            "split_entities",
            actor,
            {
                "entity_id": entity_id,
                "entity_ids": split_ids,
                "target_entity_id": target_entity_id,
            },
        )
        self._commit(state)
        return self._snapshot(state)

    def confirm_relation(self, relation_id, actor="teacher", at=None, confirmed=True):
        """Record a teacher's review decision about one relation's evidence."""
        state = self.repository.read_state()
        relation = next(
            (
                row
                for row in self._snapshot(state)["relations"]
                if row["relation_id"] == relation_id
            ),
            None,
        )
        if relation is None:
            raise ValueError(f"关系不存在：{relation_id}")
        if confirmed and relation["status"] != "resolved":
            raise ValueError("关系端点仍有歧义，不能确认")
        state["decisions"].append(
            {
                "decision_id": _next_id(state["decisions"], "decision"),
                "action": "confirm_relation",
                "record_id": relation["record_id"],
                "record_revision": relation["record_revision"],
                "relation_index": _relation_index(relation),
                "relation_key": relation["relation_key"],
                "subject_entity_id": relation["subject"]["entity_id"],
                "object_entity_id": relation["object"]["entity_id"],
                "predicate": relation["predicate"],
                "confirmed": bool(confirmed),
                "actor": actor,
                "at": _timestamp(at),
            }
        )
        self._append_event(
            state,
            "confirm_relation",
            actor,
            {
                "relation_key": relation["relation_key"],
                "confirmed": bool(confirmed),
            },
        )
        self._commit(state)
        return self.card(relation_id)

    # ----------------------------------------------------------------- audit
    def list_decisions(self):
        return self.repository.read_state()["decisions"]

    def list_events(self):
        return self.repository.read_state()["events"]

    # --------------------------------------------------------------- helpers
    def _mention_by_id(self, mention_id):
        return next(
            (
                row
                for row in self.snapshot()["mentions"]
                if row["mention_id"] == mention_id
            ),
            None,
        )

    def _commit(self, state):
        self._snapshot(state)
        self.repository.replace_state(state)

    def _append_event(self, state, action, actor, details):
        state["events"].append(
            {
                "event_id": f"event-{len(state['events']) + 1}",
                "action": action,
                "actor": actor,
                "at": _timestamp(None),
                "details": details,
            }
        )


def _analysis(record):
    return annotate(record)


def _renormalize_mentions(raw_text, mentions):
    from .knowledge import normalize_annotations

    return normalize_annotations({"text": raw_text, "mentions": list(mentions)})["mentions"]


def _content(record):
    """Canonical comparable form; annotations may be tuples or lists."""
    return json.dumps(
        {key: value for key, value in record.items() if key != "analysis"},
        ensure_ascii=False,
        sort_keys=True,
    )


def _cited_indices(card):
    indices = []
    evidence = card.get("evidence", {})
    for key in ("sentence_index", "sentence_end_index"):
        index = evidence.get(key)
        if isinstance(index, int) and index not in indices:
            indices.append(index)
    return indices


def _relation_index(relation):
    return int(relation["relation_key"].rsplit(":r", 1)[1])


def _next_id(rows, prefix):
    return f"{prefix}-{len(rows) + 1}"


def _timestamp(value):
    from .knowledge import now_iso

    return value or now_iso()
