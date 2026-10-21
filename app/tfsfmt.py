"""Turn a plain transcript into readable turns: one block per speaker, name in bold, time in grey."""
import re

_LINE = re.compile(r"^(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?(?:([A-Z][^:,.\[\]!?]{0,30}?):\s+)?(.*)$")


def parse(text: str) -> list:
    """Return [{'who': str, 'time': str, 'text': str}, ...]; consecutive lines by one speaker are merged."""
    turns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        ts, who, body = m.group(1) or "", (m.group(2) or "").strip(), m.group(3).strip()
        if who and len(who.split()) > 4:
            who, body = "", line if not ts else body
        if who:
            turns.append({"who": who, "time": ts, "text": body})
        elif turns:
            if body:
                turns[-1]["text"] += " " + body
        else:
            turns.append({"who": "", "time": ts, "text": body})
    merged = []
    for t in turns:
        if merged and t["who"] == merged[-1]["who"]:
            merged[-1]["text"] += " " + t["text"]
        else:
            merged.append(t)
    return merged


def colors(turns: list) -> dict:
    """Give each speaker a steady brand colour."""
    pal = ["#4a5e3d", "#c0562b", "#8a6a2f", "#3d5a6e", "#7a4a63", "#5f7650"]
    out = {}
    for t in turns:
        if t["who"] and t["who"] not in out:
            out[t["who"]] = pal[len(out) % len(pal)]
    return out


def to_html(text: str) -> str:
    import html
    turns = parse(text)
    col = colors(turns)
    parts = ['<div class="tx">']
    for t in turns:
        c = col.get(t["who"], "#6b5a4b")
        head = ""
        if t["who"]:
            head += '<strong style="color:' + c + '">' + html.escape(t["who"]) + '</strong>'
        if t["time"]:
            head += ' <span class="ts">' + html.escape(t["time"]) + '</span>'
        parts.append('<div class="turn" style="border-left-color:' + c + '">' + (('<div class="who">' + head + '</div>') if head else '') +
                     '<p>' + html.escape(t["text"]) + '</p></div>')
    parts.append('</div>')
    return "".join(parts)


CSS = (".tx{margin-top:10px}.turn{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--g);"
       "border-radius:10px;padding:10px 16px;margin:10px 0}.turn .who{font-size:1rem;margin-bottom:2px}"
       ".turn .ts{color:var(--mut);font-size:.82rem;font-weight:400;margin-left:6px}.turn p{margin:0;color:var(--ink);line-height:1.6}")
