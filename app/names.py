"""Fix misspellings of known participant names in a transcript (for example Jhon -> Alex)."""
import difflib
import re

WORD = re.compile(r"\b[A-Z][a-z']{2,}\b")


def canonical_tokens(names) -> list:
    out = []
    for n in names:
        for t in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", n or ""):
            t = t[0].upper() + t[1:]
            if t not in out:
                out.append(t)
    return out


def fix_names(text: str, names, cutoff: float = 0.8):
    """Return (new_text, [(old, new, count)])."""
    toks = canonical_tokens(names)
    if not toks:
        return text, []
    low = {t.lower(): t for t in toks}
    counts = {}

    def sub(m):
        w = m.group(0)
        if w.lower() in low:
            return low[w.lower()] if w != low[w.lower()] else w
        best, score = None, 0
        for t in toks:
            r = difflib.SequenceMatcher(None, w.lower(), t.lower()).ratio()
            if r > score:
                best, score = t, r
        if best and score >= cutoff and abs(len(w) - len(best)) <= 2:
            counts[(w, best)] = counts.get((w, best), 0) + 1
            return best
        return w

    new = WORD.sub(sub, text)
    return new, [(a, b, n) for (a, b), n in counts.items()]


def names_from_note(note: str) -> list:
    pats = [r"(?:name|speaker|person)[^.\n]{0,40}?\b(?:is|was|are)\s+([A-Z][\w'-]+(?: [A-Z][\w'-]+){0,2})",
            r"(?:spelled|spelt|spelling is)\s+([A-Z][\w'-]+(?: [A-Z][\w'-]+){0,2})",
            r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b"]
    out = []
    for p in pats:
        for m in re.finditer(p, note or ""):
            out.append(m.group(1))
    return out
