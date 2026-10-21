"""Shared look and navigation for every TFS meetings page (matches example.org/meetings)."""
import html

CSS = """
:root{--g:#5f7650;--gd:#4a5e3d;--t:#c0562b;--ink:#2e2118;--mut:#6b5a4b;--card:#fffdf9;--line:#e6d9c8;--soft:#f4eadc}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:#fbf7f0;line-height:1.55;font-size:1.02rem;overflow-x:hidden}
h1,h2,h3{font-family:Georgia,"Times New Roman",serif;color:var(--ink);line-height:1.2}
a{color:var(--t)}
.tfs-nav{background:#f9efe5;border-bottom:1px solid var(--line);padding:12px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px 24px;flex-wrap:wrap}
.tfs-nav .brand{font-family:Georgia,serif;font-weight:700;font-size:1.15rem;color:var(--ink);text-decoration:none}
.tfs-nav .links{display:flex;gap:4px 18px;flex-wrap:wrap}
.tfs-nav .links a{color:var(--ink);text-decoration:none;padding:4px 0;border-bottom:2px solid transparent;font-size:.98rem}
.tfs-nav .links a:hover{border-bottom-color:var(--t)}
.tfs-nav .links a.on{border-bottom-color:var(--t);font-weight:700}
.tfs-nav .links .sep{color:#b9a998}
.hero{position:relative;overflow:hidden;background:#1f2a1a url(/bg/3.jpg) center/cover;color:#fff;text-align:center;padding:64px 20px 96px}
.hero:after{content:"";position:absolute;inset:0;z-index:1;background:radial-gradient(ellipse at center,rgba(10,16,8,.38) 0%,rgba(10,16,8,.08) 70%),linear-gradient(rgba(10,16,8,.26),rgba(10,16,8,.38))}
.hero>*{position:relative;z-index:2}
.hero .eyebrow{font-size:.8rem;letter-spacing:.14em;text-transform:uppercase;color:#ffe2cf;font-weight:700;margin:0 0 10px;text-shadow:0 1px 3px rgba(0,0,0,.85)}
.hero h1{color:#fff;font-size:clamp(1.8rem,4.6vw,2.8rem);margin:0 0 12px;text-shadow:0 2px 4px rgba(0,0,0,.8),0 4px 22px rgba(0,0,0,.7)}
.hero .lead{max-width:680px;margin:0 auto;color:#fff;font-size:1.15rem;text-shadow:0 1px 3px rgba(0,0,0,.85),0 2px 14px rgba(0,0,0,.7)}
.hero a.back{display:inline-block;margin-bottom:14px;color:#ffe2cf;text-decoration:none;font-size:.92rem}
.hero a.back:hover{text-decoration:underline}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:12px}
.chip{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);border-radius:999px;padding:4px 13px;font-size:.85rem}
.wrap{max-width:1000px;margin:0 auto;padding:0 20px}
main.wrap{padding:28px 20px 56px}main.lifted{padding-top:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px 26px;box-shadow:0 6px 18px rgba(60,40,20,.08);margin-top:22px}
.card.lift{margin-top:-52px;position:relative;z-index:3;border:2px solid var(--g);box-shadow:0 10px 26px rgba(60,40,20,.14)}
.card h2{margin:0 0 6px;font-size:1.35rem}
.card h3{margin:0 0 6px;font-size:1.15rem}
.card p{color:var(--mut)}
.sub{color:var(--mut);font-size:.95rem;margin:0 0 16px}
h2{font-size:1.35rem;margin:1.4em 0 .4em}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}.tfs-nav{padding:10px 16px}}
label{display:block;margin-top:14px;font-weight:600}
input,select,textarea{width:100%;padding:11px 13px;font-size:1rem;font-family:inherit;border:1px solid #b9a998;border-radius:10px;background:#fff}
input[type=checkbox],input[type=radio]{width:auto}
input[type=number]{width:90px}
textarea{min-height:90px}
button,.btn{display:inline-block;margin:10px 6px 2px 0;padding:10px 20px;font-size:.95rem;font-weight:700;font-family:inherit;border:2px solid var(--g);border-radius:999px;background:var(--g);color:#fff;cursor:pointer;text-decoration:none}
button:hover,.btn:hover{background:var(--gd);border-color:var(--gd)}
button.alt,.btn.alt,.btn.o{background:transparent;color:var(--gd);border-color:var(--g)}
button.alt:hover,.btn.alt:hover,.btn.o:hover{background:var(--soft)}
button.danger{background:#a8431a;border-color:#a8431a;color:#fff}
button.danger:hover{background:#8a3512;border-color:#8a3512}
.btn.d{background:#a8431a;border-color:#a8431a}
button:disabled{opacity:.6;cursor:wait}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:.95rem}
th{background:var(--soft);font-size:.8rem;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
td button,td .btn{padding:6px 12px;font-size:.85rem;margin:2px 4px 2px 0}
.tablewrap{overflow-x:auto}
.pill{display:inline-block;padding:3px 12px;border-radius:999px;font-weight:700;font-size:.8rem;background:var(--soft);color:var(--mut)}
.pill.kept{background:#e1efd9;color:#2f5a1f}
.note,small{color:var(--mut);font-size:.92rem}
.box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 20px 16px}
pre{white-space:pre-wrap;font-family:inherit;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
progress{width:100%;height:18px;margin-top:18px;display:none}
#msg{margin-top:14px;font-weight:600;white-space:pre-wrap}.ok{color:#2f5a1f}.err{color:#a02020}
details{margin-top:1.2em}summary{cursor:pointer;font-weight:700}
video{width:100%;max-height:70vh;border-radius:10px;background:#000;margin-top:8px}
.tfs-foot{text-align:center;color:var(--mut);padding:10px 20px 40px;font-size:.95rem}
"""

MOD_LINKS = [
    ("start", "Start a meeting", "https://meet.example.org/"),
    ("schedule", "Schedule a meeting", "https://meet.example.org/#latercard"),
    ("about", "About", "https://example.org/meetings/"),
]
PUBLIC_LINKS = [
    ("start", "Start a meeting", "https://meet.example.org/"),
    ("schedule", "Schedule a meeting", "https://meet.example.org/#latercard"),
    ("about", "About these meetings", "https://example.org/meetings/"),
]


def nav(mode="mod", active=""):
    links = MOD_LINKS if mode == "mod" else PUBLIC_LINKS
    items = "".join('<a href="%s"%s>%s</a>' % (u, ' class="on"' if k == active else "", html.escape(t)) for k, t, u in links)
    return ('<header class="tfs-nav"><a class="brand" href="https://example.org">Example Org</a>'
            '<nav class="links">' + items + '</nav></header>')


def head(title, extra_css=""):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">'
            '<title>' + html.escape(title) + ' | Example Org</title><style>' + CSS + extra_css + '</style></head><body>')


def top(title, eyebrow="", lead="", mode="mod", active="", back=None, chips="", extra_css="", page_title=None, lift=False):
    """Everything from <!doctype> through the opening <main>. title/eyebrow/lead are plain text (escaped here)."""
    h = head(page_title or title, extra_css) + nav(mode, active)
    h += '<section class="hero">'
    if back:
        h += '<a class="back" href="%s">&larr; %s</a>' % (back[0], html.escape(back[1]))
        h += '<br>'
    if eyebrow:
        h += '<p class="eyebrow">' + html.escape(eyebrow) + '</p>'
    h += '<h1>' + html.escape(title) + '</h1>'
    if lead:
        h += '<p class="lead">' + html.escape(lead) + '</p>'
    if chips:
        h += '<div class="chips">' + chips + '</div>'
    h += '</section><main class="wrap' + (' lifted' if lift else '') + '">'
    return h


FOOT = ('</main><footer class="tfs-foot">Questions? <a href="mailto:meetings-admin@example.org">'
        'meetings-admin@example.org</a></footer>')
