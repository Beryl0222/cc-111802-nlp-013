from dataclasses import dataclass, field, asdict
from datetime import datetime


def _annotation_list(value, name):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"文本记录的 {name} 必须是列表")
    rows = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"文本记录的 {name} 项必须是对象")
        rows.append(dict(item))
    return tuple(rows)


@dataclass(frozen=True)
class TextRecord:
    """A source text together with optional, teacher-authored annotations.

    ``entities``, ``mentions`` and ``relations`` are declarations supplied with
    the record.  They are not derived state: they stay attached to the source
    revision so that derived mentions, relations and cards can be rebuilt
    deterministically while the original evidence remains reproducible.
    """

    record_id: str
    source: str
    revision: int
    observed_at: str
    text: str
    context: str = ""
    entities: tuple = field(default_factory=tuple)
    mentions: tuple = field(default_factory=tuple)
    relations: tuple = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value):
        annotations = value.get("annotations", {})
        if annotations is None:
            annotations = {}
        if not isinstance(annotations, dict):
            raise ValueError("文本记录的 annotations 必须是对象")
        entities = value.get("entities", annotations.get("entities"))
        mentions = value.get("mentions", annotations.get("mentions"))
        relations = value.get("relations", annotations.get("relations"))
        record = cls(
            record_id=str(value.get("record_id", "")).strip(),
            source=str(value.get("source", "")).strip(),
            revision=int(value.get("revision", 0)),
            observed_at=str(value.get("observed_at", "")).strip(),
            text=str(value.get("text", "")),
            context=str(value.get("context", "")).strip(),
            entities=_annotation_list(entities, "entities"),
            mentions=_annotation_list(mentions, "mentions"),
            relations=_annotation_list(relations, "relations"),
        )
        if not record.record_id or not record.source or record.revision < 1 or not record.text.strip():
            raise ValueError("文本记录缺少身份、来源、有效修订号或正文")
        datetime.fromisoformat(record.observed_at.replace("Z", "+00:00"))
        return record

    def to_dict(self):
        return asdict(self)
