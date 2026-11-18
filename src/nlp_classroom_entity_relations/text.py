import re
import unicodedata


TOKEN_RE = re.compile(r"[㐀-鿿]|[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?|[^\w\s]", re.UNICODE)
SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]*", re.UNICODE)


def normalize_text(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def tokenize(text):
    return TOKEN_RE.findall(normalize_text(text))


def split_sentences(text):
    """Return sentence segments with offsets into the normalized stored text.

    Offsets always refer to ``normalize_text(text)`` because that is the form
    persisted by :class:`TextWorkflow`.  Keeping offsets in the same
    coordinate system as the stored text is what lets a relation card point a
    teacher back at the exact sentence to re-read.
    """
    normalized = normalize_text(text)
    sentences = []
    for match in SENTENCE_RE.finditer(normalized):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        start = match.start() + leading
        end = match.end() - trailing
        sentences.append(
            {
                "index": len(sentences),
                "start": start,
                "end": end,
                "text": normalized[start:end],
            }
        )
    return sentences


def find_occurrences(text, surface):
    """Return non-overlapping offsets for ``surface`` in normalized text."""
    normalized = normalize_text(text)
    needle = normalize_text(surface)
    if not needle:
        return []
    occurrences = []
    cursor = 0
    while True:
        start = normalized.find(needle, cursor)
        if start < 0:
            return occurrences
        occurrences.append((start, start + len(needle)))
        cursor = start + len(needle)


def sentence_span(text, start, end):
    """Return indices of the sentences touched by ``[start, end)``."""
    return [
        sentence["index"]
        for sentence in split_sentences(text)
        if sentence["start"] < end and start < sentence["end"]
    ]


def annotate(record):
    tokens = tokenize(record["text"])
    unique = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    sentences = split_sentences(record["text"])
    return {
        "tokens": tokens,
        "terms": unique,
        "character_count": len(normalize_text(record["text"])),
        "token_count": len(tokens),
        "sentences": sentences,
    }
