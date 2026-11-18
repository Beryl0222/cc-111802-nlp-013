import json
from pathlib import Path


COLLECTIONS = ("records", "decisions", "events")


def empty_state():
    return {name: [] for name in COLLECTIONS}


def normalize_state(value):
    """Turn a persisted JSON value into the current state shape.

    Older state files only contained a ``records`` list.  They remain readable
    and are migrated in memory; the next write stores the full state document.
    Derived mentions, relations and cards are intentionally not persisted:
    they are recomputed from records plus human decisions on every read so a
    stale card cannot survive a correction.
    """
    if value is None:
        return empty_state()
    if not isinstance(value, dict):
        raise ValueError("记录库格式无效")
    state = empty_state()
    for name in COLLECTIONS:
        items = value.get(name, [])
        if not isinstance(items, list):
            raise ValueError(f"记录库中的 {name} 必须是列表")
        state[name] = items
    return state


class RecordRepository:
    def __init__(self, path):
        self.path = Path(path)

    def read_state(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_state()
        return normalize_state(value)

    def read(self):
        return self.read_state()["records"]

    def replace_state(self, state):
        payload = normalize_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def replace(self, records):
        """Backwards-compatible record replacement used by early callers."""
        state = self.read_state()
        state["records"] = records
        self.replace_state(state)
