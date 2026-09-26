"""BM25 keyword retrieval, in-process and dependency-free.

The corpus is a few dozen sections, so an inverted index in memory is built in
about a millisecond at cold start and needs no search service or database.
"""

import math
import re
from collections import Counter

from .corpus import Chunk

_STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself just
    me more most my no nor not now of off on once only or other our out over own same she should
    so some such than that the their them then there these they this those through to too under
    until up very was we were what when where which while who whom why will with would you your
    s t don isn wasn""".split()
)

# Operator shorthand -> the words the documents use. Applied to queries only.
_EXPANSIONS = {
    "pr": "performance ratio",
    "pi": "performance index",
    "poa": "plane array irradiance",
    "ghi": "global horizontal irradiance",
    "mlf": "marginal loss factor",
    "pid": "potential induced degradation",
    "uigf": "unconstrained intermittent generation forecast",
    "scada": "scada readings",
    "washing": "cleaning",
    "wash": "cleaning",
    "hot": "temperature",
    "heat": "temperature",
    "aemo": "aemo nem",
    "offline": "trip zero output",
    "timezone": "time zone nem time",
}


def _stem(word: str) -> str:
    """A deliberately small suffix stripper; the same rules apply to docs and queries.

    A final "e" is dropped after stripping so "price", "prices" and "priced" meet.
    """
    for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "s" and word.endswith("ss"):
                continue
            word = word[: -len(suffix)] + repl
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def tokenize(text: str, expand: bool = False) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if expand:
        words = [w for word in words for w in [word, *_EXPANSIONS.get(word, "").split()]]
    return [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1]


class BM25:
    def __init__(self, chunks: tuple[Chunk, ...], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        # Headings are strong evidence of topic, so they count twice.
        self.docs = [
            Counter(tokenize(f"{c.title} {c.section} {c.section} {c.text}")) for c in chunks
        ]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / max(len(self.lengths), 1)
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = set(tokenize(query, expand=True))
        out = []
        for doc, length in zip(self.docs, self.lengths, strict=True):
            s = 0.0
            for t in terms:
                tf = doc.get(t)
                if tf:
                    norm = self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + norm)
            out.append(s)
        return out
