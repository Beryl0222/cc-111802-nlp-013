"""Derive mentions, entity identities, relation evidence and query cards.

The module deliberately separates three layers:

* **source declarations** stored on a text record (``entities``, ``mentions``
  and ``relations``) including character offsets;
* **human decisions** such as assigning an ambiguous pronoun, merging two
  entities, splitting a wrong merge, or confirming a relation;
* **derived state** (mentions, canonical entities, relations and cards) which
  is rebuilt from the two layers above after every change.

Rebuilding instead of patching is what makes a correction propagate: a card
cannot keep pointing at an old entity because cards are regenerated from the
current mention assignments.  Evidence offsets and decision records live in
the input layer, so original positions and human decisions survive rebuilds.

Decisions are anchored to ``record_id`` plus the annotation index rather than
to a revision-qualified id.  A record revision therefore does not silently
delete a teacher's decision; instead the decision is re-evaluated and any
confirmation that no longer describes the current endpoints or version is
reported as ``needs_reconfirmation``.
"""

from datetime import datetime, timezone

from .text import find_occurrences, normalize_text, sentence_span, split_sentences

PRONOUN_SURFACES = frozenset(
    {"他", "她", "它", "他们", "她们", "它们", "其", "此", "该"}
)

ACTIVE = "active"
MERGED = "merged"
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"

REVIEW_CONFIRMED = "confirmed"
REVIEW_NEEDS_RECONFIRMATION = "needs_reconfirmation"
REVIEW_UNREVIEWED = "unreviewed"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _as_index(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


def _string_list(value, name):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} 必须是列表")
    rows = []
    for item in value:
        text = _as_text(item)
        if not text:
            raise ValueError(f"{name} 不能包含空值")
        rows.append(text)
    return rows


def mention_key(record_id, annotation_index):
    return f"{record_id}:m{annotation_index}"


def relation_key(record_id, annotation_index):
    return f"{record_id}:r{annotation_index}"


def text_version(record):
    return f"{record['record_id']}@{record['revision']}"


def normalize_annotations(record):
    """Re-anchor declared mention offsets after the text is normalized.

    Ingest normalizes the text before it is stored.  Declarations may have
    been authored against the raw text, so whenever a declared span no longer
    matches the normalized surface we relocate it by the declared occurrence
    (or the nearest occurrence).  If the surface is absent the invalid span is
    left in place and validation reports it.
    """
    text = normalize_text(record.get("text", ""))
    mentions = []
    for raw in record.get("mentions", []) or []:
        if not isinstance(raw, dict):
            mentions.append(raw)
            continue
        item = dict(raw)
        surface = normalize_text(_as_text(item.get("surface")))
        start = item.get("start")
        end = item.get("end")
        valid = (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(text)
            and text[start:end] == surface
        )
        if not valid and surface:
            occurrences = find_occurrences(text, surface)
            if occurrences:
                if isinstance(start, int):
                    chosen = min(occurrences, key=lambda span: abs(span[0] - start))
                else:
                    occurrence = item.get("occurrence", 0)
                    if isinstance(occurrence, int) and 0 <= occurrence < len(occurrences):
                        chosen = occurrences[occurrence]
                    else:
                        chosen = occurrences[0]
                item["start"], item["end"] = chosen
        mentions.append(item)
    normalized = dict(record)
    normalized["text"] = text
    normalized["mentions"] = mentions
    return normalized


def _find_span(text, surface, start=None, end=None, occurrence=0):
    normalized = normalize_text(surface)
    if not normalized:
        raise ValueError("提及缺少表面形式")
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError("提及的起止位置必须同时提供")
        start = _as_index(start, "mention.start")
        end = _as_index(end, "mention.end")
        if start < 0 or end > len(text) or start >= end:
            raise ValueError("提及位置超出正文范围")
        if text[start:end] != normalized:
            raise ValueError("提及位置与表面形式不一致")
        return start, end
    occurrences = find_occurrences(text, normalized)
    occurrence = _as_index(occurrence, "mention.occurrence")
    if occurrence < 0 or occurrence >= len(occurrences):
        raise ValueError(f"正文中找不到第 {occurrence + 1} 处提及：{normalized}")
    return occurrences[occurrence]


def _declared_entities(records):
    entities = {}
    for record in records:
        for raw in record.get("entities", []) or []:
            if not isinstance(raw, dict):
                raise ValueError("实体声明必须是对象")
            entity_id = _as_text(raw.get("entity_id"))
            label = _as_text(raw.get("label"))
            if not entity_id or not label:
                raise ValueError("实体声明缺少身份或名称")
            aliases = _string_list(raw.get("aliases"), "entity.aliases")
            existing = entities.get(entity_id)
            if existing is None:
                entities[entity_id] = {
                    "entity_id": entity_id,
                    "label": label,
                    "aliases": list(dict.fromkeys(aliases)),
                    "declared_in": [record["record_id"]],
                }
            else:
                if existing["label"] != label:
                    raise ValueError(f"实体 {entity_id} 的名称声明冲突")
                existing["aliases"] = list(
                    dict.fromkeys(existing["aliases"] + aliases)
                )
                existing["declared_in"].append(record["record_id"])
    return entities


def _prepare_mentions(record):
    """Resolve raw mention declarations to offsets without deciding identity."""
    text = record["text"]
    prepared = []
    for index, raw in enumerate(record.get("mentions", []) or []):
        if not isinstance(raw, dict):
            raise ValueError("提及声明必须是对象")
        surface = _as_text(raw.get("surface"))
        if not surface:
            raise ValueError("提及声明缺少表面形式")
        start, end = _find_span(
            text,
            surface,
            raw.get("start"),
            raw.get("end"),
            raw.get("occurrence", 0),
        )
        kind = _as_text(raw.get("kind")) or (
            "pronoun" if normalize_text(surface) in PRONOUN_SURFACES else "name"
        )
        prepared.append(
            {
                "annotation_index": index,
                "surface": surface,
                "start": start,
                "end": end,
                "kind": kind,
                "declared_entity_id": _as_text(raw.get("entity_id")) or None,
                "declared_candidates": _string_list(
                    raw.get("candidates"), "mention.candidates"
                ),
            }
        )
    return prepared


def _canonical_mapping(base_entities, decisions):
    """Apply merge/split decisions in order and return id remapping.

    ``groups`` tracks merged base identities.  A split removes the named base
    ids from the group and restores their own identity; it may also remove the
    group's current target, in which case the decision supplies a new target
    or the first remaining member takes over.  Because every relation endpoint
    is derived from its own mention's base identity and only then mapped
    through the current groups, a split can never inherit relations that
    belonged to the other person in the old merge.
    """
    groups = []
    for decision in decisions:
        action = decision.get("action")
        if action == "merge":
            member_ids = _string_list(decision.get("entity_ids"), "merge.entity_ids")
            if not member_ids:
                raise ValueError("合并实体需要至少一个实体")
            missing = [
                entity_id for entity_id in member_ids if entity_id not in base_entities
            ]
            if missing:
                raise ValueError(f"合并实体不存在：{', '.join(missing)}")
            target = _as_text(decision.get("target_entity_id")) or member_ids[0]
            if target not in member_ids:
                raise ValueError("合并目标必须包含在合并实体内")
            overlapping = [
                group for group in groups if group["members"] & set(member_ids)
            ]
            members = set(member_ids)
            for group in overlapping:
                members |= group["members"]
                groups.remove(group)
            groups.append({"members": members, "target": target})
        elif action == "split":
            entity_id = _as_text(decision.get("entity_id"))
            split_ids = _string_list(decision.get("entity_ids"), "split.entity_ids")
            if not entity_id or not split_ids:
                raise ValueError("拆分需要合并实体和待拆分实体")
            found = False
            for group in list(groups):
                if entity_id not in group["members"]:
                    continue
                removed = set(split_ids) & group["members"]
                if not removed:
                    continue
                found = True
                group["members"] -= removed
                if not group["members"]:
                    groups.remove(group)
                    continue
                requested = _as_text(decision.get("target_entity_id"))
                if group["target"] in removed:
                    if requested:
                        if requested not in group["members"]:
                            raise ValueError("拆分后的合并目标必须是保留下来的实体")
                        group["target"] = requested
                    else:
                        group["target"] = sorted(group["members"])[0]
                elif requested and requested != group["target"]:
                    raise ValueError("拆分后的合并目标必须是当前合并目标")
            if not found:
                raise ValueError(f"实体 {entity_id} 没有可拆分的合并记录")
    mapping = {}
    for group in groups:
        for member in group["members"]:
            mapping[member] = group["target"]
    return mapping, groups


def _entity_label(entity_id, base_entities):
    entity = base_entities.get(entity_id)
    return entity["label"] if entity else entity_id


def _matching_entities(surface, base_entities):
    """Match a surface form against declared names.

    A shared name is not on its own enough to resolve identity: when more than
    one entity matches, every match is reported as a candidate and the caller
    keeps the mention ambiguous rather than guessing.
    """
    matches = []
    needle = normalize_text(surface)
    for entity_id, entity in base_entities.items():
        names = {
            normalize_text(name) for name in [entity["label"], *entity["aliases"]]
        }
        if needle in names:
            matches.append(entity_id)
    return sorted(matches)


def _sentence_index(sentences, start):
    for sentence in sentences:
        if sentence["start"] <= start < sentence["end"]:
            return sentence["index"]
    return None


def _latest_assignments(decisions):
    """Latest manual mention assignment per stable (record, index) key."""
    assignments = {}
    for decision in decisions:
        if decision.get("action") != "assign_mention":
            continue
        record_id = _as_text(decision.get("record_id"))
        index = decision.get("mention_index")
        entity_id = _as_text(decision.get("entity_id"))
        if not record_id or not isinstance(index, int) or not entity_id:
            raise ValueError("人工指代决定缺少记录、提及序号或实体")
        assignments[mention_key(record_id, index)] = {
            "entity_id": entity_id,
            "surface": normalize_text(_as_text(decision.get("surface"))),
            "at_revision": decision.get("record_revision"),
        }
    return assignments


def _build_mentions(records, base_entities, mapping, assignments):
    rows = []
    by_record = {}
    for record in records:
        record_rows = []
        sentences = split_sentences(record["text"])
        for mention in _prepare_mentions(record):
            key = mention_key(record["record_id"], mention["annotation_index"])
            mention_id = (
                f"{key}@{record['revision']}"
            )
            declared = mention["declared_entity_id"]
            if declared and declared not in base_entities:
                raise ValueError(f"提及引用了未声明的实体：{declared}")
            candidates = list(dict.fromkeys(mention["declared_candidates"]))
            for candidate in candidates:
                if candidate not in base_entities:
                    raise ValueError(f"提及候选实体未声明：{candidate}")
            if not candidates and not declared:
                candidates = _matching_entities(mention["surface"], base_entities)

            manual = assignments.get(key)
            if manual is not None:
                if manual["surface"] != normalize_text(mention["surface"]):
                    # The annotation at this index changed surface, so the old
                    # decision no longer describes the current text.  Ignore it
                    # rather than silently applying it to a different person.
                    manual = None
                elif manual["entity_id"] not in base_entities:
                    raise ValueError(
                        f"人工指代决定引用了未声明的实体：{manual['entity_id']}"
                    )
            if manual is not None:
                base_entity_id = manual["entity_id"]
                assignment = "manual"
                assignment_stale = manual["at_revision"] != record["revision"]
            elif declared:
                base_entity_id = declared
                assignment = "declared"
                assignment_stale = False
            else:
                canonical_candidates = list(
                    dict.fromkeys(mapping.get(item, item) for item in candidates)
                )
                if len(canonical_candidates) == 1:
                    base_entity_id = candidates[0]
                    assignment = "alias"
                else:
                    base_entity_id = None
                    assignment = "unresolved"
                assignment_stale = False

            canonical = (
                mapping.get(base_entity_id, base_entity_id) if base_entity_id else None
            )
            canonical_candidates = list(
                dict.fromkeys(mapping.get(candidate, candidate) for candidate in candidates)
            )
            sentence_index = _sentence_index(sentences, mention["start"])
            row = {
                "mention_id": mention_id,
                "mention_key": key,
                "record_id": record["record_id"],
                "record_revision": record["revision"],
                "text_version": text_version(record),
                "annotation_index": mention["annotation_index"],
                "surface": mention["surface"],
                "start": mention["start"],
                "end": mention["end"],
                "kind": mention["kind"],
                "sentence_index": sentence_index,
                "sentence_text": (
                    sentences[sentence_index]["text"]
                    if sentence_index is not None
                    else None
                ),
                "base_entity_id": base_entity_id,
                "entity_id": canonical,
                "entity_label": (
                    _entity_label(canonical, base_entities) if canonical else None
                ),
                "candidates": canonical_candidates,
                "candidate_labels": [
                    _entity_label(candidate, base_entities)
                    for candidate in canonical_candidates
                ],
                "status": RESOLVED if canonical else AMBIGUOUS,
                "assignment": assignment if canonical else "unresolved",
                "assignment_stale": assignment_stale if canonical else False,
            }
            record_rows.append(row)
            rows.append(row)
        by_record[record["record_id"]] = record_rows
    return rows, by_record


def _latest_confirmations(decisions):
    confirmations = {}
    for decision in decisions:
        if decision.get("action") != "confirm_relation":
            continue
        record_id = _as_text(decision.get("record_id"))
        index = decision.get("relation_index")
        if not record_id or not isinstance(index, int):
            raise ValueError("关系确认决定缺少记录或关系序号")
        confirmations[relation_key(record_id, index)] = decision
    return confirmations


def _endpoint(entity_id, mention_row, base_entities):
    candidates = mention_row["candidates"] if mention_row else []
    return {
        "entity_id": entity_id,
        "label": _entity_label(entity_id, base_entities) if entity_id else None,
        "status": RESOLVED if entity_id else AMBIGUOUS,
        "candidates": candidates,
        "candidate_labels": mention_row["candidate_labels"] if mention_row else [],
    }


def _mention_by_annotation(record_mentions, index, record_id):
    for row in record_mentions:
        if row["annotation_index"] == index:
            return row
    raise ValueError(f"关系引用了不存在的提及序号 {index}（{record_id}）")


def _first_mention_of(record_mentions, entity_id, near=None):
    """Pick the mention of ``entity_id`` closest to ``near`` offsets, if any."""
    matches = [row for row in record_mentions if row["entity_id"] == entity_id]
    if not matches:
        return None
    if near is None:
        return matches[0]
    start, end = near
    return min(matches, key=lambda row: abs(row["start"] - start) + abs(row["end"] - end))


def _entity_occurrences(text, entity):
    offsets = []
    for name in [entity["label"], *entity["aliases"]]:
        offsets.extend(find_occurrences(text, name))
    return offsets


def _nearest_span(offsets, near):
    if not offsets:
        return None
    if near is None:
        return offsets[0]
    start, end = near
    return min(offsets, key=lambda span: abs(span[0] - start) + abs(span[1] - end))


def _relation_span(text, mentions, evidence, anchor=None):
    offsets = []
    if evidence:
        occurrences = find_occurrences(text, evidence)
        if not occurrences:
            raise ValueError(f"关系证据在正文中不存在：{evidence}")
        # Repeated sentences are common across lesson paragraphs.  Cite the
        # repetition closest to the relation's own mentions rather than the
        # first matching sentence in the document.
        offsets.append(_nearest_span(occurrences, anchor))
    for mention in mentions:
        offsets.append((mention["start"], mention["end"]))
    if not offsets:
        raise ValueError("关系缺少可用于定位的提及或证据")
    return min(start for start, _ in offsets), max(end for _, end in offsets)


def _build_relation(
    record,
    index,
    raw,
    record_mentions,
    base_entities,
    mapping,
    confirmations,
    sentences,
):
    if not isinstance(raw, dict):
        raise ValueError("关系声明必须是对象")
    predicate = _as_text(raw.get("predicate"))
    if not predicate:
        raise ValueError("关系声明缺少谓词")

    def endpoint_parts(prefix):
        mention = None
        if raw.get(f"{prefix}_mention") is not None:
            mention = _mention_by_annotation(
                record_mentions,
                _as_index(raw[f"{prefix}_mention"], f"{prefix}_mention"),
                record["record_id"],
            )
        declared_entity = _as_text(raw.get(f"{prefix}_entity")) or None
        if declared_entity and declared_entity not in base_entities:
            raise ValueError(f"关系引用了未声明的实体：{declared_entity}")
        # A resolved mention is the evidence anchor and therefore authoritative:
        # if a teacher corrected the pronoun's referent, the relation follows
        # it.  An explicit entity declaration only fills in an endpoint the
        # mention itself could not resolve.
        entity_id = mention["entity_id"] if mention is not None else None
        if entity_id is None:
            entity_id = declared_entity
        if mention is None and entity_id is None:
            raise ValueError(f"关系缺少{prefix}端点")
        return mention, (mapping.get(entity_id, entity_id) if entity_id else None)

    subject_mention, subject_entity = endpoint_parts("subject")
    object_mention, object_entity = endpoint_parts("object")

    evidence_mentions = [
        mention for mention in (subject_mention, object_mention) if mention is not None
    ]
    anchor = None
    if evidence_mentions:
        anchor = (
            evidence_mentions[0]["start"],
            evidence_mentions[-1]["end"],
        )
    for entity_id, mention in (
        (subject_entity, subject_mention),
        (object_entity, object_mention),
    ):
        if mention is not None or entity_id is None:
            continue
        reference = _first_mention_of(record_mentions, entity_id, anchor)
        if reference is not None:
            evidence_mentions.append(reference)

    quote_text = _as_text(raw.get("evidence"))
    if quote_text or evidence_mentions:
        start, end = _relation_span(record["text"], evidence_mentions, quote_text, anchor)
    else:
        occurrence = []
        for entity_id in (subject_entity, object_entity):
            entity = base_entities.get(entity_id)
            if entity is not None:
                occurrence.extend(_entity_occurrences(record["text"], entity))
        if not occurrence:
            raise ValueError("关系缺少可用于定位的提及或证据")
        start = min(start for start, _ in occurrence)
        end = max(end for _, end in occurrence)
    touched = sentence_span(record["text"], start, end)
    relation_id = f"{relation_key(record['record_id'], index)}@{record['revision']}"
    key = relation_key(record["record_id"], index)
    status = (
        RESOLVED
        if subject_entity is not None and object_entity is not None
        else AMBIGUOUS
    )

    mention_ids = []
    for mention in evidence_mentions:
        if mention["mention_id"] not in mention_ids:
            mention_ids.append(mention["mention_id"])

    confirmation = confirmations.get(key)
    confirmed = False
    confirmation_stale = False
    confirmed_by = None
    confirmed_at = None
    if confirmation is not None:
        confirmed_by = _as_text(confirmation.get("actor")) or None
        confirmed_at = _as_text(confirmation.get("at")) or None
        same_version = confirmation.get("record_revision") == record["revision"]
        same_endpoints = (
            confirmation.get("subject_entity_id") == subject_entity
            and confirmation.get("object_entity_id") == object_entity
            and confirmation.get("predicate") == predicate
        )
        confirms_current = (
            bool(confirmation.get("confirmed", True))
            and same_version
            and same_endpoints
            and status == RESOLVED
        )
        if confirms_current:
            confirmed = True
        elif confirmation.get("confirmed", True):
            confirmation_stale = True

    review_status = (
        REVIEW_NEEDS_RECONFIRMATION
        if confirmation_stale
        else REVIEW_CONFIRMED
        if confirmed
        else REVIEW_UNREVIEWED
    )
    evidence = {
        "start": start,
        "end": end,
        "quote": record["text"][start:end],
        "sentence_index": touched[0] if touched else None,
        "sentence_end_index": touched[-1] if touched else None,
        "sentence_texts": [
            sentences[position]["text"]
            for position in touched
            if position < len(sentences)
        ],
    }
    relation = {
        "relation_id": relation_id,
        "relation_key": key,
        "record_id": record["record_id"],
        "record_revision": record["revision"],
        "text_version": text_version(record),
        "source": record.get("source", ""),
        "predicate": predicate,
        "subject": _endpoint(subject_entity, subject_mention, base_entities),
        "object": _endpoint(object_entity, object_mention, base_entities),
        "status": status,
        "mention_ids": mention_ids,
        "evidence": evidence,
        "confirmed": confirmed,
        "confirmation_stale": confirmation_stale,
        "confirmed_by": confirmed_by if confirmed else None,
        "confirmed_at": confirmed_at if confirmed else None,
        "review_status": review_status,
    }
    relation["card"] = {
        "card_id": f"card:{relation_id}",
        "relation_id": relation_id,
        "relation_key": key,
        "record_id": record["record_id"],
        "record_revision": record["revision"],
        "text_version": relation["text_version"],
        "source": relation["source"],
        "subject": relation["subject"],
        "predicate": predicate,
        "object": relation["object"],
        "status": status,
        "evidence": evidence,
        "mention_ids": mention_ids,
        "confirmed": confirmed,
        "confirmation_stale": confirmation_stale,
        "confirmed_by": relation["confirmed_by"],
        "confirmed_at": relation["confirmed_at"],
        "review_status": review_status,
    }
    return relation


def _build_relations(records, base_entities, mapping, mention_rows, confirmations):
    relations = []
    by_record = {}
    for record in records:
        record_mentions = [
            row for row in mention_rows if row["record_id"] == record["record_id"]
        ]
        sentences = split_sentences(record["text"])
        record_relations = []
        for index, raw in enumerate(record.get("relations", []) or []):
            record_relations.append(
                _build_relation(
                    record,
                    index,
                    raw,
                    record_mentions,
                    base_entities,
                    mapping,
                    confirmations,
                    sentences,
                )
            )
        relations.extend(record_relations)
        by_record[record["record_id"]] = record_relations
    return relations, by_record


def _entity_rows(base_entities, mapping, groups):
    """Expose active entities and keep merged identities visible for audit."""
    active = {}
    for entity_id, entity in base_entities.items():
        canonical = mapping.get(entity_id, entity_id)
        if canonical == entity_id:
            active.setdefault(
                canonical,
                {
                    "entity_id": canonical,
                    "label": entity["label"],
                    "aliases": list(entity["aliases"]),
                    "status": ACTIVE,
                    "member_entity_ids": [canonical],
                },
            )
    for group in groups:
        target = group["target"]
        if target not in active:
            continue
        members = sorted(group["members"])
        if len(members) < 2:
            # A single-member group is just the restored original identity;
            # do not attach merge metadata or merged aliases to it.
            continue
        labels = []
        aliases = list(active[target]["aliases"])
        for member in members:
            entity = base_entities[member]
            if entity["label"] not in labels:
                labels.append(entity["label"])
            for alias in [*entity["aliases"], entity["label"]]:
                if alias not in aliases:
                    aliases.append(alias)
        active[target]["member_entity_ids"] = members
        active[target]["member_labels"] = labels
        active[target]["aliases"] = aliases
    rows = list(active.values())
    for entity_id, entity in base_entities.items():
        canonical = mapping.get(entity_id, entity_id)
        if canonical != entity_id:
            rows.append(
                {
                    "entity_id": entity_id,
                    "label": entity["label"],
                    "aliases": list(entity["aliases"]),
                    "status": MERGED,
                    "merged_into": canonical,
                }
            )
    return sorted(rows, key=lambda row: row["entity_id"])


def rebuild(records, decisions):
    """Rebuild all derived state for ``records`` under ``decisions``."""
    base_entities = _declared_entities(records)
    assignments = _latest_assignments(decisions)
    mapping, groups = _canonical_mapping(base_entities, decisions)
    mention_rows, _ = _build_mentions(records, base_entities, mapping, assignments)
    confirmations = _latest_confirmations(decisions)
    relations, _ = _build_relations(
        records, base_entities, mapping, mention_rows, confirmations
    )
    return {
        "entities": _entity_rows(base_entities, mapping, groups),
        "mentions": mention_rows,
        "relations": relations,
        "cards": [relation["card"] for relation in relations],
    }
