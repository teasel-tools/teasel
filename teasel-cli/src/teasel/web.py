"""Web UI for teasel — FastAPI + htmx + Pico CSS."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from . import get_version
from . import state as st
from .local_llm import (
    CF_DEFAULT_MODEL,
    CLAUDE_DEFAULT_MODEL,
    DEFAULT_TEASEL_URL,
    chat_session,
    client_from_provider,
)
from .registry import DriverDescriptor, fetch_driver, fetch_index

app = FastAPI(title="teasel", version=get_version())

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# teasel-server writes waveform CSVs, screenshots, and plots here (same default
# the server uses); serving it lets the chat inline generated plots/screenshots.
_OUTPUT_DIR = Path(os.environ.get("TEASEL_OUTPUT_DIR", "./teasel-output"))

_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@app.get("/artifacts/{name}")
def artifact(name: str) -> FileResponse:
    if not _ARTIFACT_NAME.fullmatch(name) or ".." in name:
        return HTMLResponse("bad name", status_code=400)
    path = (_OUTPUT_DIR / name).resolve()
    if path.parent != _OUTPUT_DIR.resolve() or not path.exists():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path)


# ── UI event push (server→browser) ────────────────────────────────────────────
#
# The only server→browser channel otherwise is the chat WebSocket, which is dead
# in companion mode (Claude Code is the brain, the web chat is paused). This SSE
# bus lets an MCP tool — run by either brain — push a live cue to the board view
# (e.g. show_probe_point highlighting a net). One in-process subscriber set; the
# single uvicorn worker (reload=False) makes that the correct shared state. If
# this ever runs multi-worker, /ui/event would only reach same-worker tabs.
_sse_subscribers: set[asyncio.Queue] = set()


async def _sse_broadcast(event: dict) -> None:
    """Fan an event out to every connected browser tab. A full queue (slow/dead
    tab) drops the event for that tab rather than blocking the others."""
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


@app.get("/events")
async def events() -> StreamingResponse:
    """Server-Sent Events stream the browser subscribes to with EventSource."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    _sse_subscribers.add(queue)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keep proxies/uvicorn from idling us out
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            _sse_subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Allow-list of event types an external process may push. Visual cues only — no
# state change, no secrets. Localhost-bound (the web UI default host is 127.0.0.1).
_UI_EVENT_TYPES = {"probe_point", "journal_changed"}


@app.post("/ui/event")
async def ui_event(request: Request) -> dict:
    """Ingress for an MCP tool (either brain) to push a UI cue. See _UI_EVENT_TYPES."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid JSON"}
    etype = str(body.get("type", ""))
    if etype not in _UI_EVENT_TYPES:
        return {"ok": False, "error": f"unknown event type '{etype}'"}
    if etype == "probe_point":
        net = str(body.get("net", "")).strip()
        if not net:
            return {"ok": False, "error": "probe_point needs a 'net'"}
        await _sse_broadcast({"type": "probe_point", "net": net})
    elif etype == "journal_changed":
        await _sse_broadcast({"type": "journal_changed", "path": str(body.get("path", ""))})
    return {"ok": True}


@app.post("/ui/feedback")
async def ui_feedback(request: Request) -> dict:
    """Operator → brain: append a free-text note to project.toml [ui].feedback. The
    write trips teasel-server's setup watcher, so the brain sees it on its next tool
    call (via get_setup). Keeps the human a peer, not a remote hand."""
    try:
        note = str((await request.json()).get("note", "")).strip()
    except Exception:
        return {"ok": False, "error": "invalid JSON"}
    if not note:
        return {"ok": False, "error": "empty note"}
    st.append_feedback(note, Path.cwd())
    return {"ok": True, "count": len(st.load_feedback(Path.cwd()))}


@app.get("/ui/mode")
def get_ui_mode() -> dict:
    return {"mode": st.load_ui_mode(Path.cwd())}


@app.post("/ui/mode")
async def set_ui_mode(request: Request) -> dict:
    """standalone (web chat is the brain) | companion (Claude Code is the brain).
    Persisted to project.toml [ui].mode."""
    try:
        mode = str((await request.json()).get("mode", "")).strip()
    except Exception:
        return {"ok": False, "error": "invalid JSON"}
    if mode not in ("standalone", "companion"):
        return {"ok": False, "error": "mode must be 'standalone' or 'companion'"}
    st.save_ui_mode(mode, Path.cwd())
    return {"ok": True, "mode": mode}


# ── Journal (per-project Markdown document) ───────────────────────────────────
#
# Rendered by default (marked.js, vendored offline); Edit reveals the Markdown
# source; a gallery of teasel-output plots inserts image embeds at the cursor.
# The LLM writes via the append_journal/write_journal MCP tools, which push a
# journal_changed SSE event so the open tab refreshes live. Raw string so the
# inline JS braces survive verbatim.
_JOURNAL_BODY = r"""
<style>
  #journal-toolbar{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem}
  #journal-toolbar .spacer{flex:1}
  #journal-view{border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);padding:1rem 1.25rem;background:var(--pico-card-background-color);min-height:50vh}
  #journal-view img{max-width:100%;height:auto;border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);display:block;margin:.5rem 0}
  #journal-view :first-child{margin-top:0}
  #journal-view .empty{color:var(--pico-muted-color)}
  #journal-src{width:100%;min-height:50vh;font-family:ui-monospace,monospace;font-size:.85rem}
  #journal-gallery{display:none;gap:.5rem;overflow-x:auto;padding:.5rem;border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);margin-bottom:.6rem}
  #journal-gallery figure{margin:0;flex:0 0 auto;width:120px;text-align:center}
  #journal-gallery img{width:120px;height:80px;object-fit:contain;background:var(--pico-card-background-color);border:1px solid var(--pico-table-border-color);border-radius:6px;cursor:pointer}
  #journal-gallery figcaption{font-size:.62rem;color:var(--pico-muted-color);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #journal-notice{display:none;font-size:.8rem;color:var(--pico-color-amber-550);margin-bottom:.5rem}
</style>
<hgroup>
  <h2>Journal</h2>
  <p class="muted">A structured document for this project — the assistant can write here, you can edit, and plots can be pinned in. Saved as a Markdown file beside <code>project.toml</code>.</p>
</hgroup>
<div id="journal-toolbar">
  <button id="journal-edit" style="width:auto">Edit</button>
  <button id="journal-save" class="contrast" style="width:auto;display:none">Save</button>
  <button id="journal-cancel" class="secondary outline" style="width:auto;display:none">Cancel</button>
  <span class="spacer"></span>
  <small id="journal-status" class="muted"></small>
</div>
<p id="journal-notice">The assistant updated this document. <a href="#" id="journal-reload">Discard my edits and reload</a>.</p>
<div id="journal-gallery"></div>
<div id="journal-view"><p class="empty">Nothing here yet. Click Edit to start, or ask the assistant to write it up.</p></div>
<textarea id="journal-src" style="display:none" spellcheck="true"></textarea>
<script src="/static/vendor/marked.min.js"></script>
<script>
(function(){
  var view=document.getElementById('journal-view');
  var src=document.getElementById('journal-src');
  var gallery=document.getElementById('journal-gallery');
  var notice=document.getElementById('journal-notice');
  var statusEl=document.getElementById('journal-status');
  var bEdit=document.getElementById('journal-edit');
  var bSave=document.getElementById('journal-save');
  var bCancel=document.getElementById('journal-cancel');
  var editing=false, current='';
  // Configure marked once, globally: drop raw HTML, and keep single-tilde subscripts
  // (V~out~, f~3dB~) literal instead of GFM strikethrough — only ~~double~~ strikes.
  try{
    if(window.marked && !window.__teaselMd){
      marked.use({
        renderer:{html:function(){return '';}},
        extensions:[{
          name:'tildeLiteral', level:'inline',
          start:function(src){ var i=src.indexOf('~'); return i<0?undefined:i; },
          tokenizer:function(src){
            var m=/^~(?!~)([^~\n]+)~(?!~)/.exec(src);
            return m ? {type:'text', raw:m[0], text:m[0]} : undefined;
          }
        }]
      });
      window.__teaselMd=true;
    }
  }catch(_){}
  var EMPTY='<p class="empty">Nothing here yet. Click Edit to start, or ask the assistant to write it up.</p>';

  function render(md){
    if(!md || !md.trim()){ view.innerHTML=EMPTY; return; }
    try{ view.innerHTML=marked.parse(md); }catch(e){ view.textContent=md; return; }
    // Artifacts are stored as paths relative to journal.md (so the file is portable
    // on disk). Rewrite them to the served /artifacts/<name> route so they also
    // render here in the app — by basename, which also fixes legacy /artifacts links.
    view.querySelectorAll('img').forEach(function(img){
      var base=(img.getAttribute('src')||'').split('/').pop();
      if(/^(plot_|screenshot_).+\.(svg|png)$/.test(base)){ img.setAttribute('src','/artifacts/'+base); }
    });
  }
  function load(){
    return fetch('/journal').then(function(r){return r.json();}).then(function(d){
      current=d.content||''; if(!editing){ render(current); } return current;
    }).catch(function(){});
  }
  function setEditing(on){
    editing=on;
    view.style.display=on?'none':'';
    src.style.display=on?'':'none';
    gallery.style.display=on?'flex':'none';
    bEdit.style.display=on?'none':'';
    bSave.style.display=on?'':'none';
    bCancel.style.display=on?'':'none';
    notice.style.display='none';
    if(on){ src.value=current; loadGallery(); src.focus(); }
  }
  bEdit.addEventListener('click', function(){ setEditing(true); });
  bCancel.addEventListener('click', function(){ setEditing(false); render(current); });
  bSave.addEventListener('click', function(){
    var content=src.value; statusEl.textContent='saving…'; bSave.disabled=true;
    fetch('/journal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:content})})
      .then(function(r){return r.json();}).then(function(d){
        bSave.disabled=false;
        if(d.ok){ current=content; statusEl.textContent='saved'; setEditing(false); render(current); }
        else { statusEl.textContent=d.error||'save failed'; }
      }).catch(function(){ bSave.disabled=false; statusEl.textContent='save failed'; });
  });

  function insertAtCursor(text){
    var s=src.selectionStart||0, e=src.selectionEnd||0;
    src.value=src.value.slice(0,s)+text+src.value.slice(e);
    var pos=s+text.length; src.selectionStart=src.selectionEnd=pos; src.focus();
  }
  function loadGallery(){
    fetch('/artifacts').then(function(r){return r.json();}).then(function(d){
      var arts=(d&&d.artifacts)||[];
      gallery.innerHTML='';
      if(!arts.length){ gallery.innerHTML='<small class="muted" style="padding:.3rem">No plots yet — make one in the chat.</small>'; return; }
      arts.forEach(function(a){
        var fig=document.createElement('figure');
        var img=document.createElement('img'); img.src='/artifacts/'+a.name; img.alt=a.name; img.title='Insert '+a.name;
        img.addEventListener('click', function(){ insertAtCursor('\n![]('+'/artifacts/'+a.name+')\n'); });
        var cap=document.createElement('figcaption'); cap.textContent=a.name;
        fig.appendChild(img); fig.appendChild(cap); gallery.appendChild(fig);
      });
    }).catch(function(){});
  }

  // live updates from append_journal/write_journal (or another tab saving)
  try{
    var es=new EventSource('/events');
    es.addEventListener('message', function(ev){
      var m; try{ m=JSON.parse(ev.data); }catch(_){ return; }
      if(!m || m.type!=='journal_changed') return;
      if(editing && src.value!==current){ notice.style.display=''; }  // don't clobber unsaved edits
      else { load(); }
    });
  }catch(_){}
  document.getElementById('journal-reload').addEventListener('click', function(e){
    e.preventDefault(); setEditing(false); load();
  });

  load();
  window.addEventListener('teasel:tab', function(e){ if(e.detail && e.detail.tab==='journal' && !editing){ load(); } });
})();
</script>
"""


@app.get("/journal")
def get_journal() -> dict:
    return {"content": st.load_journal(Path.cwd())}


@app.post("/journal")
async def post_journal(request: Request) -> dict:
    try:
        content = str((await request.json()).get("content", ""))
    except Exception:
        return {"ok": False, "error": "invalid JSON"}
    st.save_journal(content, Path.cwd())
    # Notify other open tabs (and any companion display) that the doc changed.
    await _sse_broadcast({"type": "journal_changed", "path": st.journal_filename(Path.cwd())})
    return {"ok": True}


def _journal_image_ref(name: str) -> str:
    """Path to an artifact *relative to the journal file*, so the embed resolves
    when journal.md is opened on disk (editor preview, GitHub, an exported
    datasheet) — not just behind the running web server's /artifacts route."""
    journal_dir = Path.cwd().resolve()
    target = (_OUTPUT_DIR / name).resolve()
    try:
        return Path(os.path.relpath(target, journal_dir)).as_posix()
    except Exception:
        return f"{_OUTPUT_DIR.name}/{name}"


@app.post("/journal/append")
async def append_journal_md(request: Request) -> dict:
    """Append to the journal directly — no LLM round-trip. Backs the chat's
    one-click 'Add to journal' buttons. Never overwrites the operator's prose
    (same separator rule as the server's append_journal tool).

    Accepts either a raw ``markdown`` snippet (assistant replies), or an
    ``artifact`` filename + optional ``caption`` (plots/screenshots) — the latter
    is rendered as a heading + image with a portable, journal-relative path."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid JSON"}

    artifact = str(data.get("artifact", "")).strip()
    if artifact:
        if not _ARTIFACT_NAME.fullmatch(artifact) or not (_OUTPUT_DIR / artifact).exists():
            return {"ok": False, "error": "unknown artifact"}
        caption = str(data.get("caption", "")).strip() or artifact
        markdown = f"### {caption}\n\n![{caption}]({_journal_image_ref(artifact)})"
    else:
        markdown = str(data.get("markdown", "")).strip()

    if not markdown:
        return {"ok": False, "error": "nothing to add"}
    existing = st.load_journal(Path.cwd())
    sep = "\n\n" if existing.strip() else ""
    st.save_journal(existing + sep + markdown + "\n", Path.cwd())
    await _sse_broadcast({"type": "journal_changed", "path": st.journal_filename(Path.cwd())})
    return {"ok": True}


_ARTIFACT_LIST_RE = re.compile(r"^(plot_.+\.(?:svg|png)|screenshot_.+\.png)$")


@app.get("/artifacts")
def list_artifacts() -> dict:
    """Plot/screenshot files in teasel-output, newest first, for the journal gallery.
    When a plot has both .svg and .png, only the .svg is listed (crisper embed)."""
    items: list[dict] = []
    if _OUTPUT_DIR.exists():
        files = [p for p in _OUTPUT_DIR.iterdir()
                 if p.is_file() and _ARTIFACT_LIST_RE.match(p.name)]
        svg_stems = {p.stem for p in files if p.suffix == ".svg"}
        files = [p for p in files
                 if not (p.suffix == ".png" and p.name.startswith("plot_") and p.stem in svg_stems)]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        items = [
            {"name": p.name, "kind": "screenshot" if p.name.startswith("screenshot_") else "plot"}
            for p in files
        ]
    return {"artifacts": items}

# Probe map — which PCB nets are wired to which instrument channels. Not ephemeral:
# it's stored per-board in project.toml ([boards."<file>".probes]), so switching
# boards swaps maps. The /pcb panel reads/writes it; each chat turn is prefixed with
# the current board's map so the LLM knows where the user is measuring/driving.
#
# Channel templates per instrument type → [(channel_key, display, default_role)].
# role: "probe" = measured/output, "input" = driven/stimulus.
_CHANNEL_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "oscilloscope": [("C1", "C1", "probe"), ("C2", "C2", "probe"),
                     ("C3", "C3", "probe"), ("C4", "C4", "probe")],
    "function-generator": [("output", "Main", "input"), ("ttl", "TTL", "input")],
}


def _probe_channels() -> list[dict]:
    """Every assignable channel across connected instruments. Empty ⇒ no
    instruments configured. (Net/role assignments live in the per-board probe map,
    not here.)"""
    out: list[dict] = []
    for inst in st.load():
        template = _CHANNEL_TEMPLATES.get(inst.type, [("main", "", "probe")])
        for ch_key, disp, default_role in template:
            out.append({
                "instrument": inst.slug,
                "channel": ch_key,
                "label": f"{inst.slug} · {disp}" if disp else inst.slug,
                "default_role": default_role,
            })
    return out


def _probe_map() -> list[dict]:
    """The active map for the currently-loaded board: net → channel/role, with a
    display label joined from the channel list."""
    rows = st.load_probes(st.load_board_file())
    if not rows:
        return []
    labels = {(c["instrument"], c["channel"]): c["label"] for c in _probe_channels()}
    out: list[dict] = []
    for r in rows:
        inst, ch = r.get("instrument", ""), r.get("channel", "")
        out.append({
            "netname": r.get("net", ""),
            "role": r.get("role", "probe"),
            "instrument": inst,
            "channel": ch,
            "label": labels.get((inst, ch)) or f"{inst} · {ch}".strip(" ·"),
        })
    return out


def _with_probes(user_input: str) -> str:
    """Prepend the probe map (if any) to a chat turn so the LLM knows which PCB nets
    are measured (probe) vs driven (input) and on which channels. A malformed
    project.toml must never break a chat turn — degrade to the bare input."""
    try:
        rows = _probe_map()
        if not rows:
            return user_input
        lines = []
        for r in rows:
            if r["role"] == "input":
                lines.append(f'- net "{r["netname"]}" is DRIVEN (input/stimulus) by {r["label"]}')
            else:
                lines.append(f'- net "{r["netname"]}" is MEASURED (probe/output) on {r["label"]}')
        body = "\n".join(lines)
        return (
            f"[probe map] The user has wired these PCB nets to lab instruments:\n{body}\n"
            f"Treat measurement/simulation questions as referring to these nets/channels "
            f"unless they say otherwise.\n\n{user_input}"
        )
    except Exception:
        return user_input


# ── Template helpers ──────────────────────────────────────────────────────────

_MOON = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
_SUN  = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'


_TAB_SCRIPT = """
  <script>
  (function(){
    var tabs=['instruments','board','chat','journal','settings'];
    function show(name){
      if(tabs.indexOf(name)<0) name='instruments';
      tabs.forEach(function(t){
        var panel=document.getElementById('tab-'+t);
        var link=document.querySelector('.tab-link[data-tab="'+t+'"]');
        if(panel) panel.classList.toggle('active', t===name);
        if(link) link.classList.toggle('active', t===name);
      });
      try{ localStorage.setItem('teasel-tab', name); }catch(e){}
      window.dispatchEvent(new CustomEvent('teasel:tab',{detail:{tab:name}}));
    }
    document.querySelectorAll('.tab-link').forEach(function(a){
      a.addEventListener('click',function(e){
        e.preventDefault(); var t=a.getAttribute('data-tab');
        history.replaceState(null,'','#'+t); show(t);
      });
    });
    function stored(){ try{ return localStorage.getItem('teasel-tab'); }catch(e){ return null; } }
    var initial=(location.hash||'').replace('#','') || stored() || 'instruments';
    show(initial);
    window.addEventListener('hashchange',function(){ show((location.hash||'').replace('#','')); });
  })();
  </script>
"""


def _page(body: str, back: str | None = None, active_tab: str | None = None) -> HTMLResponse:
    ver = get_version()
    back_html = f'<li><a href="{back}" style="font-size:.9em">← Back</a></li>' if back else ""
    nav_mid = (
        '<ul>'
        '<li><a href="#instruments" class="tab-link" data-tab="instruments">Instruments</a></li>'
        '<li><a href="#board" class="tab-link" data-tab="board">Board</a></li>'
        '<li><a href="#chat" class="tab-link" data-tab="chat">Chat</a></li>'
        '<li><a href="#journal" class="tab-link" data-tab="journal">Journal</a></li>'
        '<li><a href="#settings" class="tab-link" data-tab="settings">Settings</a></li>'
        '</ul>'
    ) if active_tab else ""
    body_class = "app-shell" if active_tab else ""
    tab_script = _TAB_SCRIPT if active_tab else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>teasel</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
  <script src="https://unpkg.com/htmx.org@2/dist/htmx.min.js"></script>
  <script>
    (function() {{
      var t = localStorage.getItem('teasel-theme') ||
        (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      document.documentElement.setAttribute('data-theme', t);
    }})();
  </script>
  <style>
    /* Compact Pico overrides */
    :root {{
      --pico-font-size: 100%;
      --pico-spacing: 0.75rem;
      --pico-form-element-spacing-vertical: 0.35rem;
      --pico-form-element-spacing-horizontal: 0.6rem;
      --pico-border-radius: 4px;
    }}
    body {{ max-width: 700px; margin: 0 auto; }}
    h2 {{ font-size: 1.15rem; margin-bottom: 0.375rem; }}
    h3 {{ font-size: 0.9rem; margin-bottom: 0.375rem; text-transform: uppercase; letter-spacing: .04em; color: var(--pico-muted-color); }}
    p {{ margin-bottom: 0.5rem; }}
    hgroup {{ margin-bottom: 1rem; }}
    hgroup > p {{ margin-top: 0.2rem; font-size: 0.85rem; }}
    header > nav {{ padding: 0.5rem 0; }}
    main {{ padding-top: 0.75rem; padding-bottom: 1.5rem; }}
    footer {{ padding: 0.75rem 0 !important; margin-top: 1.5rem !important; }}
    label {{ margin-bottom: 0.5rem; font-size: 0.875rem; }}
    input, select {{ font-size: 0.875rem; margin-bottom: 0; }}
    table {{ font-size: 0.85rem; }}
    td, th {{ padding: 0.35rem 0.5rem; }}
    hr {{ margin: 1.25rem 0; }}
    details summary {{ font-size: 0.875rem; }}
    /* Theme toggle */
    #theme-toggle {{ background: none; border: none; cursor: pointer; padding: .25rem; color: var(--pico-muted-color); line-height: 1; }}
    #theme-toggle:hover {{ color: var(--pico-color); }}
    [data-theme="dark"]  #icon-moon {{ display: none; }}
    [data-theme="light"] #icon-sun  {{ display: none; }}
    /* Tabs / app shell */
    body.app-shell {{ max-width: 1100px; }}
    nav .tab-link {{ color: var(--pico-muted-color); padding-bottom: .15rem; border-bottom: 2px solid transparent; }}
    nav .tab-link:hover {{ color: var(--pico-color); }}
    nav .tab-link.active {{ color: var(--pico-color); font-weight: 600; border-bottom-color: var(--pico-primary); }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    /* Utilities */
    .muted {{ color: var(--pico-muted-color); font-size: .85em; }}
    mark.green {{ background: none; color: var(--pico-color-jade-550); }}
    table tr.link {{ cursor: pointer; }}
    /* Compact channel grids */
    .ch-grid {{ display: grid; grid-template-columns: 2.5rem 7rem 1fr 1.75rem; gap: 0.3rem; align-items: center; margin-bottom: 0.75rem; }}
    .fg-grid {{ display: grid; grid-template-columns: 4.5rem 1fr 1.75rem; gap: 0.3rem; align-items: center; margin-bottom: 0.75rem; }}
    .ch-grid input {{ margin-bottom: 0; }}
    .ch-hdr {{ font-size: 0.7rem; font-weight: 600; color: var(--pico-muted-color); text-transform: uppercase; letter-spacing: .06em; padding-bottom: 0.25rem; border-bottom: 1px solid var(--pico-table-border-color); }}
    .ch-name {{ font-family: ui-monospace, monospace; font-size: 0.85rem; font-weight: 600; }}
    .clear-x {{ background: none; border: 1px solid var(--pico-table-border-color); border-radius: 3px; padding: 0.2rem 0.45rem; cursor: pointer; color: var(--pico-muted-color); line-height: 1; font-size: 0.8rem; flex-shrink: 0; }}
    .ch-grid .clear-x, .fg-grid .clear-x {{ width: 100%; }}
    .clear-x:hover {{ color: var(--pico-color); border-color: var(--pico-muted-color); }}
    /* Compact limit row */
    .limit-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-bottom: 0.5rem; }}
    .limit-row label {{ margin-bottom: 0; }}
  </style>
</head>
<body hx-boost="true" class="{body_class}">
  <header>
    <nav>
      <ul><li><a href="/"><strong>teasel</strong></a></li></ul>
      {nav_mid}
      <ul>
        {back_html}
        <li>
          <button id="theme-toggle" aria-label="Toggle dark mode" onclick="toggleTheme()">
            <span id="icon-moon">{_MOON}</span>
            <span id="icon-sun">{_SUN}</span>
          </button>
        </li>
      </ul>
    </nav>
  </header>
  <main>{body}</main>
  <footer style="border-top: 1px solid var(--pico-table-border-color); margin-top: 3rem; padding: 1.5rem 0; text-align: center;">
    <small class="muted">teasel v{ver}</small>
  </footer>
  <script>
    window.toggleTheme = function() {{
      var d = document.documentElement;
      var next = d.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      d.setAttribute('data-theme', next);
      localStorage.setItem('teasel-theme', next);
    }};
  </script>
  {tab_script}
</body>
</html>""")


def _banner(text: str) -> str:
    return f'<p style="color:var(--pico-color-jade-550)">✓ {text}</p>'


def _error(text: str) -> str:
    return f'<p style="color:var(--pico-color-red-550)">{text}</p>'


def _registry_rows(entries, installed: set[str], q: str = "") -> str:
    q = q.lower()
    filtered = [
        e for e in entries
        if not q or q in e.slug or q in e.name.lower()
        or q in e.manufacturer.lower() or q in e.type.lower()
    ]
    if not filtered:
        return '<tr><td colspan="5"><em>No instruments match</em></td></tr>'
    return "".join(
        f'<tr class="link" onclick="location.href=\'/registry/{e.slug}\'">'
        f'<td>{"<mark class=green>✓</mark>" if e.slug in installed else ""}</td>'
        f'<td>{e.slug}</td><td>{e.name}</td><td>{e.type}</td>'
        f'<td class="muted">{", ".join(e.interfaces)}</td>'
        f'</tr>'
        for e in filtered
    )


# ── Connected instruments (/  and  /instrument/*) ─────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(saved: str = "") -> HTMLResponse:
    """Single-page shell: Instruments / Board / Chat panels, tab-switched client-side."""
    shell = (
        f'<section id="tab-instruments" class="tab-panel">{_instruments_panel(saved)}</section>'
        f'<section id="tab-board" class="tab-panel">{_PCB_BODY}</section>'
        f'<section id="tab-chat" class="tab-panel">{_CHAT_BODY}</section>'
        f'<section id="tab-journal" class="tab-panel">{_JOURNAL_BODY}</section>'
        f'<section id="tab-settings" class="tab-panel">{_settings_panel(saved)}</section>'
    )
    return _page(shell, active_tab="instruments")


def _instruments_panel(saved: str = "") -> str:
    instruments = st.load()
    setups = {s.slug: s for s in st.load_setup()}

    banner = _banner("Configuration saved — Claude will pick it up automatically.") if saved == "1" else ""

    if not instruments:
        content = '<p>No instruments configured. <a href="/registry">Browse the registry</a> to add one.</p>'
    else:
        rows = ""
        for inst in instruments:
            conn = next(iter(inst.params.values()), "—") if inst.params else "—"
            setup = setups.get(inst.slug)
            extras = []
            if setup:
                if setup.limits:
                    extras.append(f"{len(setup.limits)} limit{'s' if len(setup.limits) > 1 else ''}")
                if setup.channels:
                    extras.append(f"{len(setup.channels)} ch")
            extras_html = f' <span class="muted">({", ".join(extras)})</span>' if extras else ""
            rows += (
                f'<tr class="link" onclick="location.href=\'/instrument/{inst.slug}\'">'
                f'<td><strong>{inst.slug}</strong></td>'
                f'<td class="muted">{inst.type or "—"}</td>'
                f'<td>{conn}{extras_html}</td>'
                f'</tr>'
            )
        content = f"""
        <table>
          <thead><tr><th>Slug</th><th>Type</th><th>Connection</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    return f"""
    {banner}
    <hgroup>
      <h2>Connected Instruments</h2>
      <p>Instruments in <code>teasel.toml</code>, available to Claude via MCP.</p>
    </hgroup>
    {content}
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
      <a href="/registry" role="button" class="outline">⊕ Add device</a>
    </div>
    """


def _settings_panel(saved: str = "") -> str:
    """Settings tab: how teasel is wired up — LLM, simulation, MCP bridge."""
    banner = _banner("Settings saved.") if saved == "settings" else ""

    # ── LLM backends (summary + manage link; full editor stays a drill-down) ──
    providers = st.load_providers()
    default = st.get_default_provider()
    if providers:
        items = "".join(
            f'<li><strong>{pc.name}</strong> '
            f'<span class="muted">{pc.type}{" · ★ default" if pc.name == default else ""}</span></li>'
            for pc in providers.values()
        )
        llm_body = f"<ul style='margin:.25rem 0 .6rem'>{items}</ul>"
    else:
        llm_body = '<p class="muted">No backends configured yet.</p>'
    llm_section = f"""
    <article>
      <h3 style="margin-top:0">LLM backends</h3>
      <p class="muted" style="font-size:.85rem">Inference backends for <code>teasel local</code>,
         stored in <code>~/.teasel/llm.toml</code>.</p>
      {llm_body}
      <a href="/settings/llm" role="button" class="outline" hx-boost="false" style="width:auto">Manage backends →</a>
    </article>"""

    # ── Simulation (netlist + SPICE backend) ──
    netlist_path = st.get_netlist_path()
    found_netlists = st.find_netlists()
    nl_datalist = nl_list_attr = ""
    if found_netlists:
        opts = "".join(f'<option value="{p.name}">' for p in found_netlists)
        nl_datalist = f'<datalist id="found-netlists">{opts}</datalist>'
        nl_list_attr = ' list="found-netlists"'
    clear_btn = (
        '<button type="submit" name="netlist_path" value="" class="secondary outline" style="width:auto">Clear</button>'
        if netlist_path else ""
    )
    ng = shutil.which("ngspice")
    spice_status = (
        f'<mark class="green">✓ ngspice</mark> <code style="font-size:.8em">{ng}</code>' if ng
        else '<span style="color:var(--pico-color-amber-550)">ngspice not found on PATH</span>'
    )
    sim_section = f"""
    <article>
      <h3 style="margin-top:0">Simulation</h3>
      <form method="post" action="/project/netlist" hx-boost="false">
        {nl_datalist}
        <label>SPICE netlist
          <small class="muted">absolute path or filename in the project directory</small>
          <input name="netlist_path" value="{netlist_path or ''}" placeholder="e.g. filter.cir"{nl_list_attr}>
        </label>
        <div style="display:flex; gap:1rem; align-items:center">
          <button type="submit" style="width:auto">Save</button>
          {clear_btn}
        </div>
      </form>
      <p class="muted" style="font-size:.85rem;margin-top:.75rem">
        SPICE backend: {spice_status}<br>
        <small>Used by net simulation (wired up in the simulate step).</small>
      </p>
    </article>"""

    # ── MCP integration (.mcp.json status; auto-generated, rewrite on demand) ──
    mcp_path = Path.cwd() / ".mcp.json"
    lab_ok = False
    if mcp_path.exists():
        try:
            lab_ok = "lab" in json.loads(mcp_path.read_text()).get("mcpServers", {})
        except Exception:
            pass
    n_inst = len(st.load())
    if not mcp_path.exists():
        mcp_status = '<span class="muted">not written yet — click Rewrite</span>'
    elif lab_ok:
        mcp_status = '<mark class="green">✓ lab server configured</mark>'
    else:
        mcp_status = '<span style="color:var(--pico-color-amber-550)">no <code>lab</code> server — click Rewrite</span>'
    mcp_section = f"""
    <article>
      <h3 style="margin-top:0">MCP (Claude) integration</h3>
      <p class="muted" style="font-size:.85rem">teasel writes <code>.mcp.json</code> so Claude Code picks up
         your lab as an MCP server. It's regenerated automatically when instruments change; Claude reads it
         from the project root.</p>
      <table><tbody>
        <tr><td class="muted">File</td><td><code style="font-size:.8em">{mcp_path}</code></td></tr>
        <tr><td class="muted">Status</td><td>{mcp_status}
            <span class="muted">· {n_inst} instrument{"s" if n_inst != 1 else ""}</span></td></tr>
      </tbody></table>
      <form method="post" action="/settings/mcp/rewrite" hx-boost="false" style="margin:0">
        <button type="submit" class="outline" style="width:auto">Rewrite .mcp.json</button>
      </form>
    </article>"""

    # ── Mode (standalone vs companion; persisted to project.toml [ui].mode) ──
    mode = st.load_ui_mode(Path.cwd())
    def _mchk(m: str) -> str:
        return "checked" if mode == m else ""
    mode_section = f"""
    <article>
      <h3 style="margin-top:0">Mode</h3>
      <p class="muted" style="font-size:.85rem">Who drives the lab. <strong>Standalone</strong>: chat here in the
         web UI. <strong>Companion</strong>: Claude Code is the brain and this UI is a live display (the web chat
         pauses). Either way you stay a peer — re-point probes or leave notes and the assistant picks them up.
         Saved to <code>project.toml</code>.</p>
      <fieldset>
        <label><input type="radio" name="ui-mode" value="standalone" {_mchk("standalone")} onchange="setMode(this.value)"> Standalone — web chat is the brain</label>
        <label><input type="radio" name="ui-mode" value="companion" {_mchk("companion")} onchange="setMode(this.value)"> Companion — Claude Code is the brain</label>
      </fieldset>
      <small id="mode-msg" class="muted"></small>
      <script>
      function setMode(m){{
        document.getElementById('mode-msg').textContent='saving…';
        fetch('/ui/mode',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{mode:m}})}})
          .then(function(r){{return r.json();}})
          .then(function(d){{ if(d.ok){{ location.reload(); }} else {{ document.getElementById('mode-msg').textContent=d.error||'failed'; }} }})
          .catch(function(){{ document.getElementById('mode-msg').textContent='failed'; }});
      }}
      </script>
    </article>"""

    return f"""
    {banner}
    <hgroup>
      <h2>Settings</h2>
      <p class="muted">How teasel is wired up — inference, simulation, and the Claude MCP bridge.</p>
    </hgroup>
    {mode_section}
    {llm_section}
    {sim_section}
    {mcp_section}
    """


@app.get("/instrument/{slug}", response_class=HTMLResponse)
def instrument_detail(slug: str, saved: str = "") -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return _page(f'<p>Instrument <code>{slug}</code> not found.</p>', back="/")

    current = next((s for s in st.load_setup() if s.slug == slug), st.InstrumentSetup(slug=slug))
    saved_note = '<span id="saved-note" style="color:var(--pico-color-jade-550);font-size:.875rem;white-space:nowrap">✓ Saved</span>' if saved else ""

    setup_fields = _setup_fields(inst, current)
    if setup_fields:
        setup_section = f"""
        <h3>Experiment setup</h3>
        <form method="post" action="/instrument/{slug}/setup"
              oninput="var n=document.getElementById('saved-note');if(n)n.remove()">
          {setup_fields}
          <div style="display:flex;align-items:center;gap:1rem">
            <button type="submit" style="width:auto;margin:0">Save setup</button>
            {saved_note}
          </div>
        </form>"""
    else:
        setup_section = '<p class="muted">No setup options for this instrument type.</p>'

    conn_rows = "".join(
        f"<tr><td class='muted'>{k}</td><td><code>{v}</code></td></tr>"
        for k, v in inst.params.items()
    ) or "<tr><td colspan='2'><em class='muted'>No connection params</em></td></tr>"

    body = f"""
    <hgroup>
      <h2>{slug}</h2>
      <p class="muted">{inst.type or "instrument"} &ensp;·&ensp; {inst.package}</p>
    </hgroup>

    {setup_section}

    <hr style="margin-top:2rem">

    <div style="display:flex; align-items:center; gap:1rem; margin-bottom:.5rem; margin-top:1.5rem">
      <h3 style="margin:0">Connection</h3>
      <a href="/instrument/{slug}/connection">Edit</a>
      <button hx-post="/instrument/{slug}/ping" hx-swap="none"
              hx-on:htmx:before-request="clearInterval(window._pingIv);var e=document.getElementById('ping-result'),n=0;e.textContent='·';window._pingIv=setInterval(function(){{n=(n+1)%3;e.textContent='···'.slice(0,n+1)}},400)"
              hx-on:htmx:after-request="clearInterval(window._pingIv);document.getElementById('ping-result').innerHTML=event.detail.xhr.responseText"
              style="width:auto;margin:0;padding:.2rem .75rem;font-size:.8rem">Ping</button>
      <span id="ping-result" class="muted" style="font-size:.85rem"></span>
    </div>
    <table><tbody>{conn_rows}</tbody></table>

    <button class="secondary outline" style="margin-top:2rem"
            onclick="document.getElementById('confirm-remove').showModal()">
      Remove instrument
    </button>

    <dialog id="confirm-remove">
      <article>
        <header>
          <button aria-label="Close" rel="prev"
                  onclick="document.getElementById('confirm-remove').close()"></button>
          <h3>Remove {slug}?</h3>
        </header>
        <p>
          This will remove <strong>{slug}</strong> from <code>teasel.toml</code>.
          The instrument can be added again at any time.
        </p>
        <footer>
          <button class="secondary"
                  onclick="document.getElementById('confirm-remove').close()">Cancel</button>
          <form method="post" action="/instrument/{slug}/delete" style="display:inline">
            <button type="submit"
                    style="background:var(--pico-color-red-550); border-color:var(--pico-color-red-550)">
              Remove
            </button>
          </form>
        </footer>
      </article>
    </dialog>
    """
    return _page(body, back="/")


@app.get("/instrument/{slug}/connection", response_class=HTMLResponse)
async def connection_form(slug: str) -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return _page(f'<p>Instrument <code>{slug}</code> not found.</p>', back="/")

    try:
        driver = await asyncio.to_thread(fetch_driver, inst.driver_slug)
        fields = _param_fields(driver, existing=inst.params)
    except Exception:
        fields = "".join(
            f'<label>{k}<input name="param_{k}" value="{v}"></label>'
            for k, v in inst.params.items()
        )

    body = f"""
    <hgroup>
      <h2>Connection — {slug}</h2>
      <p>Updates <code>teasel.toml</code></p>
    </hgroup>
    <form method="post">
      {fields}
      <button type="submit">Save connection</button>
    </form>
    """
    return _page(body, back=f"/instrument/{slug}")


@app.post("/instrument/{slug}/connection")
async def connection_save(slug: str, request: Request) -> RedirectResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    try:
        driver = await asyncio.to_thread(fetch_driver, inst.driver_slug)
        inst.params = _collect_params(driver, form)
    except Exception:
        inst.params = {k[6:]: str(v) for k, v in form.items() if k.startswith("param_")}

    st.save(instruments)
    cfg.apply(instruments)
    return RedirectResponse(f"/instrument/{slug}?saved=1", status_code=303)


@app.get("/instrument/{slug}/setup", response_class=HTMLResponse)
def instrument_setup_form(slug: str) -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return _page(f'<p>Instrument <code>{slug}</code> not found.</p>', back="/")

    current = next((s for s in st.load_setup() if s.slug == slug), st.InstrumentSetup(slug=slug))
    fields = _setup_fields(inst, current)

    if not fields:
        body = f"""
        <hgroup>
          <h2>Setup — {slug}</h2>
        </hgroup>
        <p class="muted">No setup options available for <code>{inst.type or "this instrument type"}</code>.</p>
        """
        return _page(body, back=f"/instrument/{slug}")

    body = f"""
    <hgroup>
      <h2>Setup — {slug}</h2>
      <p>Updates <code>project.toml</code></p>
    </hgroup>
    <form method="post">
      {fields}
      <button type="submit">Save setup</button>
    </form>
    """
    return _page(body, back=f"/instrument/{slug}")


@app.post("/instrument/{slug}/setup")
async def instrument_setup_save(slug: str, request: Request) -> RedirectResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    _save_setup(inst, form)
    return RedirectResponse(f"/instrument/{slug}?saved=1", status_code=303)


@app.post("/instrument/{slug}/ping")
async def instrument_ping(slug: str) -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return HTMLResponse('<span style="color:var(--pico-color-red-550)">not found</span>')
    ok, desc = await asyncio.to_thread(_ping_instrument, inst)
    if ok:
        return HTMLResponse(f'<span style="color:var(--pico-color-jade-550)">✓ reachable ({desc})</span>')
    return HTMLResponse(f'<span style="color:var(--pico-color-red-550)">✗ unreachable ({desc})</span>')


def _ping_instrument(inst: st.InstrumentConfig) -> tuple[bool, str]:
    for val in inst.params.values():
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', val) or (
            re.match(r'^[a-zA-Z][\w.-]+\.[a-zA-Z]{2,}$', val)
        ):
            ok = subprocess.run(
                ["ping", "-c", "1", "-W", "2", val],
                capture_output=True,
            ).returncode == 0
            return ok, val
        if val.startswith("/dev/"):
            return Path(val).exists(), val
    return False, "no pingable parameter"


@app.post("/instrument/{slug}/delete")
def instrument_delete(slug: str) -> RedirectResponse:
    instruments = [i for i in st.load() if i.slug != slug]
    st.save(instruments)
    cfg.apply(instruments)
    return RedirectResponse("/", status_code=303)


# ── Project settings (/project/*) ────────────────────────────────────────────

@app.post("/project/netlist")
async def set_netlist(request: Request) -> RedirectResponse:
    form = await request.form()
    path = str(form.get("netlist_path", "")).strip()
    st.save_netlist_path(path or None)
    return RedirectResponse("/?saved=settings#settings", status_code=303)


@app.post("/settings/mcp/rewrite")
def mcp_rewrite() -> RedirectResponse:
    # Regenerate .mcp.json in the project dir from the current instruments.
    cfg.apply(st.load())
    return RedirectResponse("/?saved=settings#settings", status_code=303)


# ── Registry (/registry and /registry/*) ─────────────────────────────────────

@app.get("/registry", response_class=HTMLResponse)
async def registry_browse(q: str = "") -> HTMLResponse:
    installed = {i.driver_slug for i in st.load()}
    try:
        entries = await asyncio.to_thread(fetch_index)
        rows = _registry_rows(entries, installed, q)
    except Exception as exc:
        rows = f'<tr><td colspan="5"><em>Error loading registry: {exc}</em></td></tr>'

    body = f"""
    <hgroup>
      <h2>Instrument Registry</h2>
      <p>Browse available instrument drivers.</p>
    </hgroup>
    <input type="search" name="q" value="{q}" placeholder="Search instruments…"
      hx-get="/registry/search" hx-target="#results" hx-trigger="input changed delay:200ms"
      autofocus style="margin-bottom:1rem">
    <table>
      <thead><tr><th></th><th>Slug</th><th>Name</th><th>Type</th><th>Interfaces</th></tr></thead>
      <tbody id="results">{rows}</tbody>
    </table>
    """
    return _page(body, back="/")


@app.get("/registry/search", response_class=HTMLResponse)
async def registry_search(q: str = "") -> HTMLResponse:
    installed = {i.driver_slug for i in st.load()}
    try:
        entries = await asyncio.to_thread(fetch_index)
        return HTMLResponse(_registry_rows(entries, installed, q))
    except Exception as exc:
        return HTMLResponse(f'<tr><td colspan="5"><em>Error: {exc}</em></td></tr>')


@app.get("/registry/{slug}", response_class=HTMLResponse)
async def registry_detail(slug: str) -> HTMLResponse:
    try:
        driver = await asyncio.to_thread(fetch_driver, slug)
    except ValueError as exc:
        return _page(f"<p>{exc}</p>", back="/registry")
    except Exception as exc:
        return _page(f"<p>Error: {exc}</p>", back="/registry")

    installed = {i.driver_slug for i in st.load()}

    pkg = ("Driver: bundled in <code>teasel-server</code>"
           if driver.package == "teasel-server"
           else f"Driver: <code>uvx --with {driver.package} teasel-server</code>")

    steps_html = ""
    if driver.setup_steps:
        items = "".join(f"<li>{s}</li>" for s in driver.setup_steps)
        steps_html = f"<h3>Setup steps</h3><ol>{items}</ol>"

    manual_html = (f'<p>Manual: <a href="{driver.manual}" target="_blank">{driver.manual}</a></p>'
                   if driver.manual else "")

    add_label = "Add another instance →" if slug in installed else "Add this instrument →"
    add_btn = f'<a href="/add/{slug}" role="button">{add_label}</a>'

    body = f"""
    <hgroup>
      <h2>{driver.name}</h2>
      <p>{driver.type} &ensp;·&ensp; {", ".join(driver.interfaces)}</p>
    </hgroup>
    <p>{pkg}</p>
    {manual_html}
    {steps_html}
    <div style="margin-top:2rem">{add_btn}</div>
    """
    return _page(body, back="/registry")


# ── Add flow (/add/*  and  /setup/*) ─────────────────────────────────────────

@app.get("/add/{slug}", response_class=HTMLResponse)
async def add_form(slug: str, error: str = "") -> HTMLResponse:
    try:
        driver = await asyncio.to_thread(fetch_driver, slug)
    except ValueError as exc:
        return _page(f"<p>{exc}</p>", back="/registry")
    except Exception as exc:
        return _page(f"<p>Error loading driver: {exc}</p>", back="/registry")

    default_name = st.next_instance_name(slug)
    err_html = _error(error) if error else ""

    body = f"""
    <hgroup>
      <h2>Add {driver.name}</h2>
      <p>Step 1 of 2 — connection settings → <code>teasel.toml</code></p>
    </hgroup>
    {err_html}
    <form method="post">
      <label>
        Instance name
        <small class="muted">unique name for this device</small>
        <input name="instance_name" value="{default_name}" required>
      </label>
      {_param_fields(driver)}
      <button type="submit">Save and continue →</button>
    </form>
    """
    return _page(body, back=f"/registry/{slug}")


@app.post("/add/{slug}")
async def add_save(slug: str, request: Request) -> RedirectResponse:
    form = await request.form()
    try:
        driver = await asyncio.to_thread(fetch_driver, slug)
    except Exception:
        return RedirectResponse(f"/add/{slug}?error=Failed+to+load+driver", status_code=303)

    inst_slug = str(form.get("instance_name", slug)).strip() or slug
    params = _collect_params(driver, form)

    instruments = [i for i in st.load() if i.slug != inst_slug]
    instruments.append(st.InstrumentConfig(
        slug=inst_slug,
        driver=slug if slug != inst_slug else "",
        package=driver.package,
        type=driver.type,
        params=params,
    ))
    st.save(instruments)
    cfg.apply(instruments)
    return RedirectResponse(f"/setup/{inst_slug}", status_code=303)


@app.get("/setup/{slug}", response_class=HTMLResponse)
def setup_form(slug: str) -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return RedirectResponse("/", status_code=303)

    current = next((s for s in st.load_setup() if s.slug == slug), st.InstrumentSetup(slug=slug))
    fields = _setup_fields(inst, current)

    if not fields:
        return RedirectResponse("/?saved=1", status_code=303)

    body = f"""
    <hgroup>
      <h2>Setup {slug}</h2>
      <p>Step 2 of 2 — experiment settings → <code>project.toml</code>
        <span class="muted">(optional)</span></p>
    </hgroup>
    <form method="post">
      {fields}
      <div style="display:flex; gap:1rem; align-items:center">
        <button type="submit">Save setup</button>
        <a href="/?saved=1">Skip</a>
      </div>
    </form>
    """
    return _page(body, back="/")


@app.post("/setup/{slug}")
async def setup_save(slug: str, request: Request) -> RedirectResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    _save_setup(inst, form)
    return RedirectResponse("/?saved=1", status_code=303)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/instruments")
def api_instruments() -> list[dict]:
    return [
        {"slug": i.slug, "type": i.type, "package": i.package, "params": i.params}
        for i in st.load()
    ]


# ── Chat (/chat and /ws/chat) ─────────────────────────────────────────────────
#
# In-browser chat that drives the connected instruments. The forge loop in
# local_llm.py runs the teasel-server MCP tools; its on_message events are
# streamed here as JSON envelopes over a WebSocket and rendered as bubbles
# (user/assistant), monospace tool-call/result traces, guardrail lines, and
# errors. Raw string so the inline CSS/JS braces and `\n` survive verbatim into
# the page.

_CHAT_BODY = r"""
<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;flex-wrap:wrap">
  <hgroup style="margin:0">
    <h2 style="margin:0">Lab chat</h2>
    <p class="muted" id="chat-status" style="margin:0">connecting…</p>
  </hgroup>
  <label style="margin:0;font-size:.78rem;color:var(--pico-muted-color);display:flex;align-items:center;gap:.4rem;white-space:nowrap">
    Model
    <select id="chat-model" style="margin:0;padding:.15rem .45rem;font-size:.78rem;width:auto;background-position:right .4rem center"></select>
  </label>
</div>
<p id="chat-probe" class="muted" style="margin:-.4rem 0 .5rem;font-size:.82rem"></p>
<style>
  #chat-log{height:55vh;overflow-y:auto;border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);padding:.75rem;display:flex;flex-direction:column;gap:.5rem;margin-bottom:.75rem;background:var(--pico-card-background-color)}
  #chat-log .msg{max-width:85%;padding:.4rem .65rem;border-radius:10px;font-size:.9rem;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}
  #chat-log .user{align-self:flex-end;background:var(--pico-primary-background);color:var(--pico-primary-inverse);border-bottom-right-radius:3px}
  #chat-log .assistant{align-self:flex-start;background:var(--pico-code-background-color);border-bottom-left-radius:3px}
  /* Markdown-rendered assistant bubble: normal wrapping + tamed block spacing so
     **bold**, lists, headings and `code` look right inside the bubble. */
  #chat-log .assistant.md{white-space:normal}
  #chat-log .assistant.md > :first-child{margin-top:0}
  #chat-log .assistant.md > :last-child{margin-bottom:0}
  #chat-log .assistant.md p{margin:.4rem 0}
  #chat-log .assistant.md ul,#chat-log .assistant.md ol{margin:.4rem 0;padding-left:1.25rem}
  #chat-log .assistant.md li{margin:.1rem 0}
  #chat-log .assistant.md h1,#chat-log .assistant.md h2,#chat-log .assistant.md h3,#chat-log .assistant.md h4{font-size:1em;font-weight:700;margin:.5rem 0 .2rem}
  #chat-log .assistant.md code{background:var(--pico-card-background-color);padding:.05rem .3rem;border-radius:4px;font-size:.85em}
  #chat-log .assistant.md pre{background:var(--pico-card-background-color);border:1px solid var(--pico-table-border-color);border-radius:6px;padding:.5rem .6rem;margin:.4rem 0;overflow-x:auto}
  #chat-log .assistant.md pre code{background:none;padding:0}
  #chat-log .assistant.md a{color:var(--pico-primary)}
  #chat-log .assistant.md blockquote{margin:.4rem 0;padding-left:.6rem;border-left:2px solid var(--pico-table-border-color);color:var(--pico-muted-color)}
  #chat-log .assistant.md table{border-collapse:collapse;margin:.4rem 0;font-size:.85em}
  #chat-log .assistant.md th,#chat-log .assistant.md td{border:1px solid var(--pico-table-border-color);padding:.2rem .4rem}
  #chat-log .trace{align-self:flex-start;font-family:ui-monospace,monospace;font-size:.72rem;color:var(--pico-muted-color);white-space:pre-wrap;word-break:break-all;max-width:100%}
  #chat-log .guardrail{align-self:flex-start;font-size:.75rem;color:var(--pico-color-amber-550)}
  #chat-log .error{align-self:flex-start;font-size:.82rem;color:var(--pico-color-red-550)}
  #chat-log .empty{margin:auto;color:var(--pico-muted-color);font-size:.85rem;text-align:center}
  #chat-log .artifact{align-self:flex-start;max-width:92%}
  #chat-log .artifact img{display:block;max-width:100%;border-radius:10px;border:1px solid var(--pico-table-border-color)}
  #chat-log .to-journal{align-self:flex-start;width:auto;margin:.15rem 0 .2rem;padding:.1rem .5rem;font-size:.72rem;line-height:1.5;background:transparent;border:1px solid var(--pico-primary);color:var(--pico-primary)}
  #chat-log .to-journal:hover{background:var(--pico-primary);color:var(--pico-primary-inverse)}
  #chat-log .to-journal.added{border-color:var(--pico-color-jade-550);color:var(--pico-color-jade-550);background:transparent;cursor:default}
  /* Caption modal — a small styled popup, replaces the native window.prompt. */
  #cap-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);backdrop-filter:blur(2px);display:none;align-items:center;justify-content:center;z-index:1000}
  #cap-overlay.open{display:flex;animation:capfade .12s ease}
  @keyframes capfade{from{opacity:0}to{opacity:1}}
  #cap-modal{background:var(--pico-card-background-color);border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);padding:1.25rem 1.4rem 1.1rem;width:min(28rem,92vw);box-shadow:0 16px 48px rgba(0,0,0,.4);animation:capin .16s cubic-bezier(.2,.8,.3,1)}
  @keyframes capin{from{opacity:0;transform:translateY(8px) scale(.97)}to{opacity:1;transform:none}}
  #cap-modal h4{margin:0 0 .1rem;font-size:1rem}
  #cap-modal .cap-sub{font-size:.8rem;color:var(--pico-muted-color);margin:0 0 .85rem}
  #cap-modal .cap-preview{display:flex;align-items:center;gap:.6rem;margin:0 0 .85rem;padding:.4rem;border:1px solid var(--pico-table-border-color);border-radius:8px;background:var(--pico-code-background-color)}
  #cap-modal .cap-preview img{width:64px;height:44px;object-fit:contain;border-radius:4px;flex:0 0 auto}
  #cap-modal .cap-preview small{color:var(--pico-muted-color);overflow-wrap:anywhere}
  #cap-modal input{margin:0 0 1rem}
  #cap-modal .cap-actions{display:flex;gap:.5rem;justify-content:flex-end}
  #cap-modal .cap-actions button{width:auto;margin:0}
</style>
<div id="chat-log"><div class="empty">Ask teasel to read or drive a connected instrument.</div></div>
<form id="chat-form" autocomplete="off" hx-boost="false">
  <fieldset role="group" style="margin:0">
    <input id="chat-input" name="msg" placeholder="e.g. set the function generator to 10 kHz" disabled>
    <button id="chat-send" type="submit" disabled style="width:auto">Send</button>
  </fieldset>
</form>
<div id="cap-overlay">
  <div id="cap-modal" role="dialog" aria-modal="true" aria-labelledby="cap-title">
    <h4 id="cap-title">Add to journal</h4>
    <p class="cap-sub">Give this capture a caption.</p>
    <div class="cap-preview"><img id="cap-thumb" alt=""><small id="cap-file"></small></div>
    <input id="cap-input" autocomplete="off" placeholder="Caption">
    <div class="cap-actions">
      <button type="button" id="cap-cancel" class="secondary outline">Cancel</button>
      <button type="button" id="cap-add">Add to journal</button>
    </div>
  </div>
</div>
<script>
(function(){
  var log=document.getElementById('chat-log');
  var form=document.getElementById('chat-form');
  var input=document.getElementById('chat-input');
  var send=document.getElementById('chat-send');
  var statusEl=document.getElementById('chat-status');
  var probeEl=document.getElementById('chat-probe');
  function refreshProbe(){
    fetch('/pcb/probes').then(function(r){return r.json();}).then(function(d){
      var rows=(d&&d.probes)||[];
      if(!rows.length){ probeEl.textContent=''; return; }
      var parts=rows.map(function(p){ return p.netname+'→'+(p.channel||p.instrument); });
      probeEl.textContent='📍 '+rows.length+' net'+(rows.length>1?'s':'')+' mapped — '+parts.join(', ')+' — attached to your messages.';
    }).catch(function(){});
  }
  refreshProbe();
  window.addEventListener('teasel:probes-changed', refreshProbe);
  // refresh whenever the Chat tab becomes active, however we got here
  window.addEventListener('teasel:tab', function(e){ if(e.detail && e.detail.tab==='chat') refreshProbe(); });
  function add(cls,text){
    var empty=log.querySelector('.empty'); if(empty){empty.remove();}
    var d=document.createElement('div'); d.className=cls; d.textContent=text;
    log.appendChild(d); log.scrollTop=log.scrollHeight; return d;
  }
  function busy(b){ input.disabled=b; send.disabled=b; if(!b){input.focus();} }
  // One-click capture into the journal — appends Markdown directly (no LLM round-trip).
  function journalPost(payload, btn){
    if(btn){ btn.disabled=true; }
    fetch('/journal/append',{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(payload)})
      .then(function(r){return r.json();})
      .then(function(d){
        if(d.ok){ if(btn){ btn.textContent='✓ in journal'; btn.classList.add('added'); } }
        else { if(btn){ btn.textContent='failed'; btn.disabled=false; } }
      })
      .catch(function(){ if(btn){ btn.textContent='failed'; btn.disabled=false; } });
  }
  function journalAppend(markdown, btn){ journalPost({markdown:markdown}, btn); }
  // A '+ Journal' button that appends fixed Markdown (used for assistant replies).
  function journalButton(markdown, label){
    var b=document.createElement('button'); b.type='button'; b.className='to-journal';
    b.textContent=label||'+ Journal'; b.title='Add this to the project journal';
    b.addEventListener('click', function(){
      if(!(markdown||'').trim()){ return; }
      journalAppend(markdown, b);
    });
    return b;
  }
  // Styled caption modal (replaces native window.prompt). promptCaption resolves
  // to the caption string, or null if the user cancelled.
  var _capEls=null, _capResolve=null;
  function _capModal(){
    if(_capEls){ return _capEls; }
    var o=document.getElementById('cap-overlay');
    _capEls={o:o, input:document.getElementById('cap-input'),
             add:document.getElementById('cap-add'), cancel:document.getElementById('cap-cancel'),
             thumb:document.getElementById('cap-thumb'), file:document.getElementById('cap-file')};
    function done(val){ o.classList.remove('open'); var r=_capResolve; _capResolve=null; if(r){ r(val); } }
    _capEls.add.addEventListener('click', function(){ done(_capEls.input.value); });
    _capEls.cancel.addEventListener('click', function(){ done(null); });
    o.addEventListener('click', function(e){ if(e.target===o){ done(null); } });
    _capEls.input.addEventListener('keydown', function(e){
      if(e.key==='Enter'){ e.preventDefault(); done(_capEls.input.value); }
      else if(e.key==='Escape'){ e.preventDefault(); done(null); }
    });
    return _capEls;
  }
  function promptCaption(def, name){
    var el=_capModal();
    el.thumb.src='/artifacts/'+name; el.file.textContent=name;
    el.input.value=def;
    var p=new Promise(function(resolve){ _capResolve=resolve; });
    el.o.classList.add('open');
    setTimeout(function(){ el.input.focus(); el.input.select(); }, 0);
    return p;
  }
  // A '+ Journal' button for a plot/screenshot — opens the caption modal (prefilled
  // with a timestamp, so Enter is enough) and pins it under a heading.
  function artifactJournalButton(name, isShot){
    var b=document.createElement('button'); b.type='button'; b.className='to-journal';
    b.textContent='+ Journal'; b.title='Add this '+(isShot?'screenshot':'plot')+' to the journal';
    b.addEventListener('click', function(){
      if(b.classList.contains('added')){ return; }
      var def=(isShot?'Screenshot — ':'Plot — ')+new Date().toLocaleString();
      promptCaption(def, name).then(function(cap){
        if(cap===null){ return; }          // cancelled
        // Server builds the heading + a journal-relative image path (portable).
        journalPost({caption:cap.trim()||def, artifact:name}, b);
      });
    });
    return b;
  }
  // Tool results that saved a plot or screenshot get the image inlined, each with
  // a button to pin it into the journal.
  function addArtifacts(text){
    var re=/\b(plot_[A-Za-z0-9_]+\.svg|screenshot_[A-Za-z0-9._-]+\.png)\b/g, m, seen={};
    while((m=re.exec(text||''))!==null){
      if(seen[m[1]]){continue;} seen[m[1]]=true;
      var name=m[1];
      var d=document.createElement('div'); d.className='artifact';
      var img=document.createElement('img'); img.src='/artifacts/'+name; img.alt=name;
      img.onload=function(){ log.scrollTop=log.scrollHeight; };
      img.onerror=function(){ d.remove(); };
      d.appendChild(img);
      d.appendChild(artifactJournalButton(name, name.indexOf('screenshot_')===0));
      log.appendChild(d); log.scrollTop=log.scrollHeight;
    }
  }
  // Configure marked once, globally (it's a singleton shared with the Journal):
  // drop raw HTML, and stop single-tilde subscripts/approximations the models emit
  // (V~out~, f~3dB~, ~10~20 ms) from rendering as GFM strikethrough — only real
  // ~~double~~ tildes strike through. Guarded so it registers exactly once.
  function setupMarked(){
    if(!window.marked){ return false; }
    if(!window.__teaselMd){
      marked.use({
        renderer:{html:function(){return '';}},
        extensions:[{
          name:'tildeLiteral', level:'inline',
          start:function(src){ var i=src.indexOf('~'); return i<0?undefined:i; },
          tokenizer:function(src){
            var m=/^~(?!~)([^~\n]+)~(?!~)/.exec(src);
            return m ? {type:'text', raw:m[0], text:m[0]} : undefined;
          }
        }]
      });
      window.__teaselMd=true;
    }
    return true;
  }
  // Assistant text is Markdown (models emit **bold**, lists, `code`, …). Render it
  // with marked (the same lib the Journal uses, raw HTML dropped); fall back to the
  // plain textContent set by add() if marked is unavailable or parsing fails.
  function renderMarkdown(el, text){
    if(!setupMarked()){ return; }
    try{
      el.innerHTML=marked.parse(text); el.classList.add('md');
      // Models often embed the plot they just made as a path relative to the project
      // (teasel-output/plot_….png). Serve it via /artifacts/<basename>, as the Journal does.
      el.querySelectorAll('img').forEach(function(img){
        var base=(img.getAttribute('src')||'').split('/').pop();
        if(/^(plot_|screenshot_).+\.(svg|png)$/.test(base)){ img.setAttribute('src','/artifacts/'+base); }
        img.onerror=function(){ img.remove(); };
      });
    }
    catch(_){ /* keep the plain-text fallback */ }
  }
  // Assistant reply bubble, with a button to capture its text into the journal.
  function addAssistant(text){
    var d=add('msg assistant', text);   // textContent — safe fallback
    if((text||'').trim()){
      renderMarkdown(d, text);
      log.appendChild(journalButton(text)); log.scrollTop=log.scrollHeight;
    }
  }
  var proto=location.protocol==='https:'?'wss:':'ws:';
  var ws=null, modelSel=document.getElementById('chat-model');
  var EMPTY_CHAT='Ask teasel to read or drive a connected instrument.';
  function render(m){
    if(m.type==='ready'){ statusEl.textContent=m.provider+' · '+m.model+' · '+m.tools+' tools'; busy(false); }
    else if(m.type==='history'){
      // Rebuild the log to match the server's persisted transcript: restores a
      // conversation across reload/navigation, or clears it on a fresh chat.
      log.innerHTML=''; (m.events||[]).forEach(render);
      if(!log.children.length){ log.innerHTML='<div class="empty">'+EMPTY_CHAT+'</div>'; }
      log.scrollTop=log.scrollHeight;
    }
    else if(m.type==='user'){ add('msg user', m.text); }
    else if(m.type==='text'){ addAssistant(m.text); }
    else if(m.type==='tool_call'){ m.calls.forEach(function(c){ add('trace','→ '+c.name+'('+JSON.stringify(c.args)+')'); }); }
    else if(m.type==='tool_result'){ add('trace','← '+(m.text||'').split('\n')[0].slice(0,200)); addArtifacts(m.text); }
    else if(m.type==='guardrail'){ add('guardrail','⚠ '+m.label+': '+(m.text||'').split('\n')[0].slice(0,200)); }
    else if(m.type==='error'){ add('error', m.text); busy(false); }
    else if(m.type==='done'){ busy(false); }
  }
  function onmessage(e){ render(JSON.parse(e.data)); }
  // The chat reads its backend from resolve_provider (project pin → global
  // default), so connect() takes no provider arg — picking a model persists the
  // pin server-side first, then we reconnect to pick it up.
  function connect(){
    if(ws){ ws.onclose=null; try{ ws.close(); }catch(_){} }
    busy(true); statusEl.textContent='connecting…';
    ws=new WebSocket(proto+'//'+location.host+'/ws/chat?brain=local');
    ws.onclose=function(){ busy(true); statusEl.textContent='disconnected — reload to reconnect'; };
    ws.onerror=function(){ statusEl.textContent='connection error'; };
    ws.onmessage=onmessage;
  }
  function companionBanner(){
    statusEl.textContent='Companion mode — Claude Code is driving; chat paused.';
    var empty=log.querySelector('.empty');
    if(empty){ empty.textContent='Companion mode: Claude Code is the brain. Switch to Standalone in Settings to chat here.'; }
    busy(true);
  }
  // Populate the picker; the effective backend (project pin or global default) is
  // preselected. Choosing one pins it to this project's teasel.toml — it persists
  // across restarts — then reconnects the chat on it.
  function startChat(){
   fetch('/llm/providers').then(function(r){return r.json();}).then(function(d){
    var provs=(d&&d.providers)||[];
    if(modelSel && provs.length){
      modelSel.innerHTML='';
      // One option per (backend, model): a backend with several models contributes
      // several rows. Provider+model ride on data-attrs so labels stay readable.
      provs.forEach(function(p){
        var ms=(p.models&&p.models.length)?p.models:[p.model||''];
        ms.forEach(function(mid){
          var o=document.createElement('option');
          o.value=p.name+'␟'+mid;
          o.setAttribute('data-provider',p.name); o.setAttribute('data-model',mid);
          o.textContent=p.name+(mid?' — '+mid:'');
          if(p.name===d.effective && (mid===d.effective_model || (!d.effective_model&&mid===(p.model||'')))){ o.selected=true; }
          modelSel.appendChild(o);
        });
      });
      modelSel.addEventListener('change',function(){
        var opt=modelSel.options[modelSel.selectedIndex];
        var name=opt.getAttribute('data-provider'), mid=opt.getAttribute('data-model');
        fetch('/llm/project-provider',{method:'POST',headers:{'Content-Type':'application/json'},
                                       body:JSON.stringify({name:name,model:mid})})
          .then(function(r){return r.json();})
          .then(function(d){
            if(d.ok){ add('guardrail','📌 pinned '+name+(mid?' · '+mid:'')+' to this project — starting a fresh chat'); connect(); }
            else { add('error', d.error||'could not pin model'); }
          })
          .catch(function(){ add('error','could not save model choice'); });
      });
    }
    connect();
   }).catch(function(){ connect(); });
  }
  // Companion mode: Claude Code is the brain — don't open a chat WS to :7891.
  fetch('/ui/mode').then(function(r){return r.json();}).then(function(d){
    if(d && d.mode==='companion'){ companionBanner(); } else { startChat(); }
  }).catch(function(){ startChat(); });
  form.addEventListener('submit',function(ev){
    ev.preventDefault();
    var text=input.value.trim();
    if(!text||!ws||ws.readyState!==1){ return; }
    // The server echoes the user message back (type:"user") as the single source
    // of truth, so it lands in the persisted transcript and replays on reconnect.
    ws.send(text);
    input.value=''; busy(true);
  });
})();
</script>
"""


@app.get("/chat")
def chat_page() -> RedirectResponse:
    # Chat now lives in the single-page shell; keep the old URL working.
    return RedirectResponse("/#chat", status_code=307)


# ── persistent chat room ──────────────────────────────────────────────────────
#
# The forge ChatSession holds an MCP/anyio connection that is *task-affinity
# bound* — it must be entered, used, and exited on one task. So a single
# long-lived owner task owns the session; WebSocket connections are thin
# subscribers. This keeps the conversation alive across page reloads and
# navigation: a reconnecting browser gets the transcript replayed AND the live
# LLM context is untouched. The room rebuilds when the provider/model pin
# changes (a deliberate "fresh chat") or on connection failure.

# Event types that make up the visible transcript (replayed on reconnect).
# "ready"/"done"/"history" are connection-level, not conversation, so excluded.
_PERSIST_TYPES = {"user", "text", "tool_call", "tool_result", "guardrail"}
_TRANSCRIPT_CAP = 800  # bound memory on very long sessions


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


class ChatRoom:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._subs: set[WebSocket] = set()
        self._transcript: list[dict] = []
        self._ready: dict | None = None
        self._error: str | None = None
        self._key: tuple | None = None
        self._started: asyncio.Event = asyncio.Event()

    async def attach(self, websocket: WebSocket, pc, model_override) -> bool:
        """Subscribe a socket; (re)start the owner if needed; replay transcript.
        Returns False if the session is unavailable (error already sent)."""
        key = (pc.name, model_override or "")
        async with self._lock:
            if self._owner is None or self._owner.done() or self._key != key:
                await self._stop_locked()
                self._key = key
                self._ready = None
                self._error = None
                self._transcript = []
                self._started = asyncio.Event()
                self._inbox = asyncio.Queue()
                self._owner = asyncio.create_task(self._run(pc, model_override))
            await self._started.wait()
            if self._error is not None:
                await _safe_send(websocket, {"type": "error", "text": self._error})
                return False
            self._subs.add(websocket)
            await _safe_send(websocket, self._ready)
            # Always send history (even empty) so the browser rebuilds its log to
            # match the server — restoring a conversation, or clearing it on a
            # fresh-chat repin.
            await _safe_send(websocket, {"type": "history", "events": self._transcript})
            return True

    def detach(self, websocket: WebSocket) -> None:
        self._subs.discard(websocket)

    async def submit(self, text: str) -> None:
        await self._inbox.put(text)

    def _record(self, ev: dict) -> None:
        if ev.get("type") in _PERSIST_TYPES:
            self._transcript.append(ev)
            if len(self._transcript) > _TRANSCRIPT_CAP:
                del self._transcript[: len(self._transcript) - _TRANSCRIPT_CAP]

    async def _broadcast(self, ev: dict) -> None:
        for ws in list(self._subs):
            if not await _safe_send(ws, ev):
                self._subs.discard(ws)

    async def _emit(self, ev: dict) -> None:
        self._record(ev)
        await self._broadcast(ev)

    async def _run(self, pc, model_override) -> None:
        try:
            client, model = client_from_provider(pc, model_override)
            async with chat_session(client, DEFAULT_TEASEL_URL) as session:
                self._ready = {
                    "type": "ready",
                    "provider": pc.name,
                    "model": model,
                    "tools": session.tool_count,
                }
                self._started.set()
                while True:
                    text = await self._inbox.get()
                    if text is None:
                        return
                    await self._emit({"type": "user", "text": text})
                    await session.run_turn(_with_probes(text), self._emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = (
                f"Could not reach teasel-server at {DEFAULT_TEASEL_URL} ({exc}). "
                "Start it with: teasel-server --transport streamable-http"
            )
            self._started.set()  # unblock any waiting attach()
            await self._broadcast({"type": "error", "text": self._error})

    async def _stop_locked(self) -> None:
        if self._owner is not None and not self._owner.done():
            await self._inbox.put(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._owner), timeout=10)
        self._owner = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()


_room = ChatRoom()


@app.on_event("shutdown")
async def _shutdown_chat_room() -> None:
    await _room.stop()


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    await websocket.accept()

    # ?brain= seam: only the local forge brain is wired today; the Claude Agent
    # SDK is a separate increment (different dep + auth path).
    if websocket.query_params.get("brain", "local") != "local":
        await websocket.send_json({
            "type": "error",
            "text": "Only the local provider brain is wired up right now.",
        })
        await websocket.close()
        return

    # The chat model picker pins its choice in teasel.toml (persistent), which
    # resolve_provider reads as the project pin. ?provider= remains a one-off
    # session override (e.g. for other callers); an unknown name falls through
    # to None below.
    provider_override = websocket.query_params.get("provider") or None
    pc = st.resolve_provider(provider_override, Path.cwd())
    if pc is None:
        which = f" '{provider_override}'" if provider_override else ""
        await websocket.send_json({
            "type": "error",
            "text": f"No LLM backend{which} configured. Add one under LLM settings, then reload.",
        })
        await websocket.close()
        return

    # When using the project pin (no ?provider= override), honor the pinned model
    # too, so a one-backend-many-models setup runs the chosen model.
    model_override = None if provider_override else st.load_project_model(Path.cwd())
    if not await _room.attach(websocket, pc, model_override):
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    try:
        while True:
            user_input = (await websocket.receive_text()).strip()
            if user_input:
                await _room.submit(user_input)
    except WebSocketDisconnect:
        pass
    finally:
        _room.detach(websocket)


# ── PCB probe (/pcb) ──────────────────────────────────────────────────────────
#
# Loads a .kicad_pcb into the patched kicanvas viewer (vendored bundle, KiCad 9
# named-net + probe-only patch). The board renders entirely client-side — only the
# clicked net name is POSTed to /pcb/probes and written into project.toml, then folded
# into the next lab-chat turn via _with_probes(). Raw string so the inline JS braces
# survive verbatim.

_PCB_BODY = r"""
<style>
  #pcb-toolbar{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem}
  #pcb-main{display:flex;gap:1rem;align-items:stretch;flex-wrap:wrap}
  #pcb-view{flex:1 1 460px;min-width:0;height:66vh;border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);overflow:hidden;background:var(--pico-card-background-color)}
  #pcb-view kicanvas-embed{display:block;width:100%;height:100%}
  #pcb-view .ph{padding:1rem;color:var(--pico-muted-color);font-size:.85rem}
  #pcb-view.probe-pulse{animation:boardpulse 1.6s ease}
  @keyframes boardpulse{0%,100%{border-color:var(--pico-table-border-color)}25%,60%{border-color:var(--pico-primary);box-shadow:0 0 0 2px var(--pico-primary-background)}}
  /* fixed-width panel so labels don't reflow the layout as nets/channels change */
  #probe-panel{flex:0 0 340px;max-width:340px;min-width:0;display:flex;flex-direction:column}
  #probe-panel h3{margin-bottom:.5rem}
  #probe-table{font-size:.82rem;margin-bottom:.5rem;table-layout:fixed;width:100%}
  #probe-table td{padding:.25rem .35rem;vertical-align:middle}
  #probe-table .net{width:7rem;font-family:ui-monospace,monospace;font-weight:600;color:var(--pico-color-jade-550);overflow-wrap:anywhere}
  #probe-table .role-col{width:4.8rem}
  #probe-table .x-col{width:1.7rem}
  #probe-table select{font-size:.78rem;padding:.15rem .3rem;margin:0;width:100%;max-width:100%}
  #probe-table .clear-x{width:100%}
  #probe-table tr.flash{animation:probeflash 1s ease}
  @keyframes probeflash{from{background:var(--pico-primary-background)}to{background:transparent}}
  #probe-empty,#probe-nochan{font-size:.8rem;margin:.25rem 0 .75rem}
  label.file-btn{margin:0}
</style>
<hgroup>
  <h2>PCB probe</h2>
  <p class="muted">Load a board, then click nets — pads, tracks, vias — and assign each to an instrument channel as a probe (measured) or input (driven).</p>
</hgroup>
<div id="pcb-toolbar">
  <label class="file-btn" role="button">
    Load .kicad_pcb…
    <input id="pcb-file" type="file" accept=".kicad_pcb" hidden>
  </label>
  <span id="pcb-name" class="muted"></span>
  <button id="pcb-clear-board" class="secondary outline" style="width:auto;padding:.1rem .55rem;font-size:.8rem;display:none">Clear board</button>
</div>
<div id="pcb-main">
  <div id="pcb-view"><p class="ph">No board loaded.</p></div>
  <div id="probe-panel">
    <h3>Probe map</h3>
    <table id="probe-table"><tbody></tbody></table>
    <p id="probe-empty" class="muted">Click a net on the board to add it here.</p>
    <p id="probe-nochan" class="muted" style="display:none">Add an instrument under the Instruments tab to assign channels.</p>
    <span style="flex:1"></span>
    <div id="feedback-box" style="margin-top:.6rem;border-top:1px solid var(--pico-table-border-color);padding-top:.5rem">
      <small class="muted" style="display:block;margin-bottom:.3rem">Note to the assistant (e.g. "moved probe to TP4"):</small>
      <fieldset role="group" style="margin:0">
        <input id="feedback-input" placeholder="tell the assistant something…" style="font-size:.8rem;margin:0">
        <button id="feedback-send" class="outline" style="width:auto;font-size:.8rem;margin:0">Send</button>
      </fieldset>
      <small id="feedback-msg" class="muted" style="font-size:.72rem"></small>
    </div>
    <a href="#chat" role="button" class="outline" style="width:auto;margin-top:.5rem">Open lab chat →</a>
  </div>
</div>
<script type="module" src="/static/vendor/kicanvas.js"></script>
<script>
(function(){
  var view=document.getElementById('pcb-view');
  var nameEl=document.getElementById('pcb-name');
  var clearBoardBtn=document.getElementById('pcb-clear-board');
  var fileInput=document.getElementById('pcb-file');
  var tbody=document.querySelector('#probe-table tbody');
  var emptyEl=document.getElementById('probe-empty');
  var nochanEl=document.getElementById('probe-nochan');

  // ── operator → assistant: free-text note (reverse half of the loop) ──
  var fbInput=document.getElementById('feedback-input');
  var fbSend=document.getElementById('feedback-send');
  var fbMsg=document.getElementById('feedback-msg');
  function sendFeedback(){
    var note=(fbInput.value||'').trim();
    if(!note) return;
    fbSend.disabled=true; fbMsg.textContent='sending…';
    fetch('/ui/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:note})})
      .then(function(r){return r.json();})
      .then(function(d){ fbSend.disabled=false;
        if(d.ok){ fbInput.value=''; fbMsg.textContent='sent — the assistant sees it on its next step.'; }
        else { fbMsg.textContent=d.error||'failed'; } })
      .catch(function(){ fbSend.disabled=false; fbMsg.textContent='failed'; });
  }
  if(fbSend){ fbSend.addEventListener('click', sendFeedback); }
  if(fbInput){ fbInput.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); sendFeedback(); } }); }

  // ── probe-map state (source of truth = project.toml via /pcb/probes) ──
  var channels=[];   // [{instrument, channel, label, default_role, ...}]
  var probes=[];     // [{netname, role, instrument, channel}]
  var chanKey=function(c){ return c.instrument+'::'+c.channel; };

  function usedKeys(exceptNet){
    return probes.filter(function(p){return p.netname!==exceptNet;})
                 .map(function(p){return p.instrument+'::'+p.channel;});
  }
  var roleOf=function(c){ return c.default_role || 'probe'; };
  function channelsForRole(role){
    return channels.filter(function(c){ return roleOf(c)===role; });
  }
  function firstFreeChannelForRole(role){
    var used=usedKeys(null);
    return channelsForRole(role).filter(function(c){ return used.indexOf(chanKey(c))<0; })[0] || null;
  }
  function save(){
    fetch('/pcb/probes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(probes)})
      .then(function(r){return r.json();})
      .then(function(d){ probes=d.probes||[]; renderTable();
        window.dispatchEvent(new CustomEvent('teasel:probes-changed')); })
      .catch(function(){});
  }
  function opt(value,label,selected,disabled){
    var o=document.createElement('option'); o.value=value; o.textContent=label;
    if(selected)o.selected=true; if(disabled)o.disabled=true; return o;
  }
  function renderTable(){
    tbody.innerHTML='';
    probes.forEach(function(p){
      var tr=document.createElement('tr'); tr.setAttribute('data-net',p.netname);
      var tdN=document.createElement('td'); tdN.className='net'; tdN.textContent=p.netname; tr.appendChild(tdN);
      var tdR=document.createElement('td'); tdR.className='role-col';
      var rs=document.createElement('select'); rs.className='role-sel';
      rs.appendChild(opt('probe','Probe',p.role==='probe',false));
      rs.appendChild(opt('input','Input',p.role==='input',false));
      rs.addEventListener('change',function(){
        p.role=rs.value;
        // role drives the channel list — switch to a valid channel if the current
        // one can't serve this role (e.g. a scope can't be an input).
        var curKey=p.instrument+'::'+p.channel;
        var curOk=channels.some(function(c){ return chanKey(c)===curKey && roleOf(c)===p.role; });
        if(!curOk){
          var free=firstFreeChannelForRole(p.role) || channelsForRole(p.role)[0] || null;
          p.instrument=free?free.instrument:''; p.channel=free?free.channel:'';
        }
        save();
      });
      tdR.appendChild(rs); tr.appendChild(tdR);
      var tdC=document.createElement('td');
      var cs=document.createElement('select'); cs.className='chan-sel';
      var used=usedKeys(p.netname), curKey=p.instrument+'::'+p.channel;
      var pick=channelsForRole(p.role);
      // keep the current channel visible even if it doesn't match the role
      if(p.instrument && !pick.some(function(c){return chanKey(c)===curKey;})){
        var cur=channels.filter(function(c){return chanKey(c)===curKey;})[0];
        if(cur) pick=[cur].concat(pick);
      }
      if(!pick.length){ cs.appendChild(opt('','— no '+p.role+' channels —',true,true)); }
      pick.forEach(function(c){
        var key=chanKey(c), sel=(key===curKey);
        cs.appendChild(opt(key, c.label, sel, !sel && used.indexOf(key)>=0));
      });
      cs.disabled=!channels.length || !pick.length;
      cs.addEventListener('change',function(){
        var k=cs.value.split('::'); p.instrument=k[0]||''; p.channel=k[1]||''; save();
      });
      tdC.appendChild(cs); tr.appendChild(tdC);
      var tdX=document.createElement('td'); tdX.className='x-col';
      var bx=document.createElement('button'); bx.className='clear-x'; bx.title='Remove'; bx.textContent='×';
      bx.addEventListener('click',function(){
        probes=probes.filter(function(q){return q.netname!==p.netname;}); save();
      });
      tdX.appendChild(bx); tr.appendChild(tdX);
      tbody.appendChild(tr);
    });
    emptyEl.style.display=probes.length?'none':'';
    nochanEl.style.display=channels.length?'none':'';
  }
  function flashRow(net){
    var tr=tbody.querySelector('tr[data-net="'+(window.CSS&&CSS.escape?CSS.escape(net):net)+'"]');
    if(tr){ tr.classList.remove('flash'); void tr.offsetWidth; tr.classList.add('flash'); }
  }
  function addProbe(netname){
    if(!netname) return;
    if(probes.some(function(p){return p.netname===netname;})){ flashRow(netname); return; }
    if(!channels.length){ nochanEl.style.display=''; return; }
    // clicking a net to add it = probing by default; grab a free probe channel,
    // else any free channel (and adopt its natural role).
    var role='probe', c=firstFreeChannelForRole('probe');
    if(!c){ c=channelsForRole('probe')[0]; }
    if(!c){ var any=channels.filter(function(x){return usedKeys(null).indexOf(chanKey(x))<0;});
            c=any[0]||channels[0]; role=c?roleOf(c):'probe'; }
    probes.push({netname:netname, role:role,
                 instrument:c?c.instrument:'', channel:c?c.channel:''});
    save();
  }
  // load channels + the current board's map (call again after a board change)
  function loadProbes(){
    return fetch('/pcb/probes').then(function(r){return r.json();}).then(function(d){
      channels=d.channels||[]; probes=d.probes||[]; renderTable();
      window.dispatchEvent(new CustomEvent('teasel:probes-changed'));
    }).catch(function(){});
  }
  loadProbes();

  // ── live cues pushed from an MCP tool (show_probe_point) over SSE ──
  // EventSource auto-reconnects on drop, so no manual reconnect logic is needed.
  function highlightNet(net){
    if(!net) return;
    flashRow(net);                              // flash the net's row in the probe table
    if(view){                                   // and pulse the board frame as a board-level cue
      view.classList.remove('probe-pulse'); void view.offsetWidth; view.classList.add('probe-pulse');
    }
  }
  try{
    var es=new EventSource('/events');
    es.addEventListener('message', function(ev){
      var m; try{ m=JSON.parse(ev.data); }catch(_){ return; }
      if(m && m.type==='probe_point' && m.net){ highlightNet(m.net); }
    });
    es.onerror=function(){ /* browser auto-reconnects */ };
  }catch(_){}

  // ── kicanvas board: render, persist (IndexedDB), lazy on tab activation ──
  function deepFind(selector, root){
    root=root||document;
    var d=root.querySelector(selector); if(d) return d;
    var all=root.querySelectorAll('*');
    for(var i=0;i<all.length;i++){ if(all[i].shadowRoot){ var f=deepFind(selector, all[i].shadowRoot); if(f) return f; } }
    return null;
  }
  var isNetInfo=function(o){ return o && typeof o==='object' && 'net' in o
      && o.__lookupGetter__ && o.__lookupGetter__('netname')!==undefined; };

  var installedOn=null;
  function installWrap(){
    var tries=0;
    (function poll(){
      var vEl=deepFind('kc-board-viewer');
      var viewer=vEl && vEl.viewer;
      if(viewer && viewer.board && viewer.handleItemClick && installedOn!==viewer){
        var orig=viewer.handleItemClick.bind(viewer);
        viewer.handleItemClick=function(item){
          try{ orig(item); }catch(e){ console.warn('[pcb] viewer threw:', e.message); }
          try{ if(isNetInfo(item) && item.netname!=null) addProbe(item.netname); }
          catch(e){ console.warn('[pcb] probe extract failed:', e.message); }
        };
        // single click always resolves (auto-pick first on overlap)
        viewer.contextMenuCallback=function(x,y,items,onSelect){ onSelect(items.values().next().value); };
        installedOn=viewer;
        return;
      }
      if(tries++<80) setTimeout(poll,100);
    })();
  }
  function renderBoard(text, name){
    nameEl.textContent=name||'';
    installedOn=null;
    var embed=document.createElement('kicanvas-embed');
    embed.setAttribute('controls','full');
    var src=document.createElement('kicanvas-source');
    src.setAttribute('name', name && name.endsWith('.kicad_pcb')?name:(name||'board')+'.kicad_pcb');
    src.textContent=text;
    embed.appendChild(src);
    view.replaceChildren(embed);
    clearBoardBtn.style.display='inline-block';
    installWrap();
    setTimeout(function(){ window.dispatchEvent(new Event('resize')); },60);
  }
  function placeholder(){
    var p=document.createElement('p'); p.className='ph'; p.textContent='No board loaded.';
    view.replaceChildren(p);
  }

  // Board persists in the project: stored as a real .kicad_pcb file and referenced
  // from project.toml [board]. Loaded lazily on first Board-tab activation so the
  // WebGL canvas isn't sized while hidden.
  var savedBoard=null, rendered=false, boardActive=false;
  function maybeRender(){
    if(boardActive && !rendered && savedBoard && savedBoard.content){
      renderBoard(savedBoard.content, savedBoard.name); rendered=true;
    }
    if(boardActive){ setTimeout(function(){ window.dispatchEvent(new Event('resize')); },60); }
  }
  window.addEventListener('teasel:tab', function(e){
    boardActive=!!(e.detail && e.detail.tab==='board'); maybeRender();
  });
  fetch('/pcb/board').then(function(r){return r.json();}).then(function(d){
    if(d && d.name && d.content){ savedBoard=d; maybeRender(); }
  }).catch(function(){});

  fileInput.addEventListener('change', async function(e){
    var file=e.target.files[0]; if(!file) return;
    var text=await file.text();
    savedBoard={name:file.name, content:text};
    renderBoard(text, file.name); rendered=true;
    fetch('/pcb/board',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(savedBoard)})
      .then(function(r){return r.json();})
      .then(function(d){ if(d && d.name){ savedBoard.name=d.name; nameEl.textContent=d.name; }
        // board changed → load that board's probe map (empty if new, restored if seen)
        return loadProbes(); })
      .catch(function(){});
  });
  clearBoardBtn.addEventListener('click', function(){
    fetch('/pcb/board/clear',{method:'POST'}).then(function(){ return loadProbes(); }).catch(function(){});
    savedBoard=null; rendered=false; installedOn=null;
    placeholder(); nameEl.textContent=''; clearBoardBtn.style.display='none';
  });
})();
</script>
"""


@app.get("/pcb")
def pcb_page() -> RedirectResponse:
    # Board now lives in the single-page shell; keep the old URL working.
    return RedirectResponse("/#board", status_code=307)


@app.get("/llm/providers")
def llm_providers() -> dict:
    """Configured LLM backends for the chat model picker. Secrets are never
    included — only name/type/model, the saved global default, and this project's
    pinned provider (teasel.toml [llm].provider). `effective` is what the chat
    will actually use: the project pin if set, else the global default."""
    default = st.get_default_provider()
    project = st.load_project_provider(Path.cwd())
    project_model = st.load_project_model(Path.cwd())
    providers = list(st.load_providers().values())
    out = [
        {
            "name": pc.name,
            "type": pc.type,
            "model": pc.model or "",
            # A backend may expose several models (one key, many models); fall back
            # to its single default model so every backend offers at least one.
            "models": pc.models or ([pc.model] if pc.model else []),
        }
        for pc in providers
    ]
    eff_provider = project or default
    eff = next((p for p in out if p["name"] == eff_provider), None)
    eff_model = project_model or (eff["model"] if eff else "") \
        or (eff["models"][0] if eff and eff["models"] else "")
    return {
        "default": default,
        "project": project,
        "effective": eff_provider,
        "effective_model": eff_model,
        "providers": out,
    }


@app.post("/llm/project-provider")
async def set_project_provider(request: Request) -> dict:
    """Pin this project's chat backend + model in teasel.toml ([llm] provider/model)
    so it persists across sessions. The global default stays the fallback for
    unpinned projects."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    model = (body.get("model") or "").strip()
    if name and name not in st.load_providers():
        return {"ok": False, "error": f"Unknown backend '{name}'."}
    st.save_project_provider(name or None, Path.cwd(), model=model or None)
    return {"ok": True, "provider": name or None, "model": model or None}


@app.post("/llm/anthropic/models")
async def anthropic_models(request: Request) -> dict:
    """List models from the Anthropic API to populate the settings model picker.

    The key comes from the form (just typed) or, when editing with the field left
    blank, from the named saved backend. It's used only to call Anthropic and is
    never logged or returned."""
    import urllib.error
    import urllib.request

    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        name = (body.get("name") or "").strip()
        saved = st.load_providers().get(name) if name else None
        key = saved.api_key if saved else ""
    if not key:
        return {"ok": False, "error": "No API key — enter one above first."}

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models?limit=1000",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = "invalid API key" if e.code == 401 else f"HTTP {e.code}"
        return {"ok": False, "error": f"Anthropic API: {detail}"}
    except Exception as e:  # network/DNS/timeout
        return {"ok": False, "error": f"Could not reach Anthropic API ({e})."}

    models = [
        {"id": m.get("id", ""), "display_name": m.get("display_name") or m.get("id", "")}
        for m in data.get("data", [])
        if m.get("id")
    ]
    return {"ok": True, "models": models}


@app.get("/pcb/probes")
def get_probes() -> dict:
    return {"probes": _probe_map(), "channels": _probe_channels()}


@app.post("/pcb/probes")
async def set_probes(request: Request) -> dict:
    """Replace the probe map for the currently-loaded board. Body is a list of
    {netname, role, instrument, channel}; written to project.toml under that board."""
    payload = await request.json()
    rows = payload if isinstance(payload, list) else payload.get("probes", [])

    board = st.load_board_file()
    if not board:
        return {"ok": False, "probes": []}

    # Only accept (instrument, channel) pairs that are real channels on connected
    # instruments — enforces "strictly from instruments" and keeps untrusted client
    # values out of the stored map. One net ↔ one channel; role clamped.
    valid = {(c["instrument"], c["channel"]) for c in _probe_channels()}
    clean: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        net = (row.get("netname") or "").strip()
        inst = (row.get("instrument") or "").strip()
        ch = (row.get("channel") or "").strip()
        role = "input" if (row.get("role") or "").strip() == "input" else "probe"
        if not net or net in seen or (inst, ch) not in valid:
            continue
        seen.add(net)
        clean.append({"net": net, "instrument": inst, "channel": ch, "role": role})

    st.save_probes(board, clean)
    return {"ok": True, "probes": _probe_map()}


# ── PCB board file (stored in the project, referenced from project.toml [board]) ──

@app.get("/pcb/board")
def get_board() -> dict:
    """Return the project board (filename + content) referenced by project.toml, if any."""
    name = st.load_board_file()
    if not name:
        return {"name": None}
    # Defend against a hand-edited project.toml with a traversal path — only serve a
    # plain .kicad_pcb from the project dir.
    name = Path(name).name
    if not name.endswith(".kicad_pcb"):
        return {"name": None}
    path = Path.cwd() / name
    if not path.exists():
        return {"name": None}
    try:
        return {"name": name, "content": path.read_text(errors="replace")}
    except Exception:
        return {"name": None}


@app.post("/pcb/board")
async def set_board(request: Request) -> dict:
    """Persist an uploaded board: write it as a real .kicad_pcb in the project dir and
    record [board].file in project.toml. Board content stays out of teasel.toml/project.toml."""
    data = await request.json()
    name = Path((data.get("name") or "").strip()).name  # strip any path components
    content = data.get("content") or ""
    if not name or not content:
        return {"ok": False}
    if not name.endswith(".kicad_pcb"):
        name += ".kicad_pcb"
    (Path.cwd() / name).write_text(content)
    st.save_board_file(name)
    return {"ok": True, "name": name}


@app.post("/pcb/board/clear")
def clear_board() -> dict:
    # Clear the project reference; leave the .kicad_pcb file on disk.
    st.save_board_file(None)
    return {"ok": True}


# ── Form field helpers ────────────────────────────────────────────────────────

def _param_fields(driver: DriverDescriptor, existing: dict | None = None) -> str:
    existing = existing or {}
    html = ""
    for param in driver.params:
        name = param.param or param.key.lower()
        value = existing.get(name, "")
        if not value and param.default is not None:
            value = param.default
        elif not value and param.required:
            value = param.example or ""
        label = f"{param.key}{' <small style=color:var(--pico-color-red-550)>*</small>' if param.required else ''}"
        placeholder = param.example or ""
        required = "required" if param.required else ""
        html += f"""
        <label>
          {label}
          <small class="muted">{param.description}</small>
          <input name="param_{name}" value="{value}" placeholder="{placeholder}" {required}>
        </label>"""
    return html


def _collect_params(driver: DriverDescriptor, form) -> dict[str, str]:
    params: dict[str, str] = {}
    for param in driver.params:
        name = param.param or param.key.lower()
        value = str(form.get(f"param_{name}", "")).strip()
        if value:
            params[name] = value
        elif param.default is not None:
            params[name] = param.default
    return params


def _node_datalist(nodes: list[str]) -> tuple[str, str]:
    """Return (datalist_html, list_attr) for node suggestions, or ('', '') if no nodes."""
    if not nodes:
        return "", ""
    opts = "".join(f'<option value="{n}">' for n in nodes)
    # Clear on focus so all options show; restore on blur if nothing new was typed
    js = ' onfocus="this._v=this.value;this.value=\'\'" onblur="if(!this.value)this.value=this._v"'
    return f'<datalist id="netlist-nodes">{opts}</datalist>', f' list="netlist-nodes"{js}'


def _load_netlist_nodes() -> list[str]:
    netlist = st.get_netlist_path()
    if not netlist:
        return []
    try:
        return st.parse_netlist_nodes(netlist)
    except Exception:
        return []


def _setup_fields(inst: st.InstrumentConfig, current: st.InstrumentSetup) -> str:
    nodes = _load_netlist_nodes()
    datalist, list_attr = _node_datalist(nodes)
    html = datalist
    if inst.type == "function-generator":
        fg_rows = ""
        for ch, ch_label in (("output", "Main"), ("ttl", "TTL")):
            lbl = current.channels.get(ch, {}).get("label", "")
            fg_rows += f"""
          <div class="ch-name">{ch_label}</div>
          <input name="ch_{ch}_label" value="{lbl}" placeholder="—"{list_attr}>
          <button type="button" class="clear-x" title="Clear"
                  onclick="document.querySelector('[name=ch_{ch}_label]').value=''">×</button>"""
        limit_inputs = ""
        for key, label, kind in st.LIMIT_FIELDS:
            val = current.limits.get(key, "")
            if kind == "number":
                limit_inputs += f"""
          <label>{label}
            <input name="limit_{key}" type="number" step="any" value="{val}" placeholder="no limit">
          </label>"""
            else:
                limit_inputs += f"""
          <label style="grid-column:1/-1">{label}
            <input name="limit_{key}" value="{val}" placeholder="e.g. TLE2142 input abs-max ±15 V">
          </label>"""
        html += f"""
        <div class="fg-grid">
          <div class="ch-hdr">Output</div>
          <div class="ch-hdr">Label</div>
          <div></div>
          {fg_rows}
        </div>
        <div class="limit-row">
          {limit_inputs}
        </div>
        <small class="muted">Safety limits are enforced by teasel-server before any command reaches the
          instrument — the assistant is refused, told why, and cannot override them.</small>"""
    if inst.type == "oscilloscope":
        rows = ""
        for ch in ("C1", "C2", "C3", "C4"):
            ch_cfg = current.channels.get(ch, {})
            probe = ch_cfg.get("probe", "")
            label = ch_cfg.get("label", "")
            rows += f"""
          <div class="ch-name">{ch}</div>
          <input name="ch_{ch}_probe" value="{probe}" placeholder="e.g. 10x">
          <input name="ch_{ch}_label" value="{label}" placeholder="—"{list_attr}>
          <button type="button" class="clear-x" title="Clear"
                  onclick="document.querySelector('[name=ch_{ch}_label]').value=''">×</button>"""
        html += f"""
        <div class="ch-grid">
          <div class="ch-hdr">Ch</div>
          <div class="ch-hdr">Probe</div>
          <div class="ch-hdr">Label</div>
          <div></div>
          {rows}
        </div>"""
    return html


def _save_setup(inst: st.InstrumentConfig, form) -> None:
    previous = next((s.limits for s in st.load_setup() if s.slug == inst.slug), {})
    limits = st.limits_from_form(
        {key: str(form.get(f"limit_{key}", "")) for key, _, _ in st.LIMIT_FIELDS}, previous,
    )

    # Channels hold only bench tuning (label/probe). Net/role live in the per-board
    # probe map (project.toml [boards.*]), so there's nothing to merge here.
    channels: dict[str, dict] = {}
    if inst.type == "function-generator":
        for ch in ("output", "ttl"):
            lbl = str(form.get(f"ch_{ch}_label", "")).strip()
            if lbl:
                channels[ch] = {"label": lbl}
    else:
        for ch in ("C1", "C2", "C3", "C4"):
            cfg: dict = {}
            probe = str(form.get(f"ch_{ch}_probe", "")).strip()
            lbl = str(form.get(f"ch_{ch}_label", "")).strip()
            if probe:
                cfg["probe"] = probe
            if lbl:
                cfg["label"] = lbl
            if cfg:
                channels[ch] = cfg

    existing = [s for s in st.load_setup() if s.slug != inst.slug]
    existing.append(st.InstrumentSetup(slug=inst.slug, limits=limits, channels=channels))
    st.save_setup(existing)


# ── LLM backends (/settings/llm) ─────────────────────────────────────────────

def _provider_list_html() -> str:
    providers = st.load_providers()
    default = st.get_default_provider()
    if not providers:
        return '<p class="muted">No backends configured yet — add one below.</p>'
    rows = ""
    for pc in providers.values():
        is_default = pc.name == default
        star = ' <small style="color:var(--pico-primary)">★ default</small>' if is_default else ""
        set_default = "" if is_default else f"""
            <form method="post" action="/settings/llm/default" style="display:inline;margin:0">
              <input type="hidden" name="name" value="{pc.name}">
              <button class="outline" style="padding:.1rem .5rem;font-size:.7rem;margin:0">Set default</button>
            </form>"""
        rows += f"""
        <tr>
          <td><strong>{pc.name}</strong>{star}</td>
          <td>{pc.type}</td>
          <td><code style="font-size:.75rem">{pc.model or '—'}</code></td>
          <td style="text-align:right;white-space:nowrap">
            <a href="/settings/llm?edit={pc.name}" role="button" class="outline" style="padding:.1rem .5rem;font-size:.7rem">Edit</a>
            {set_default}
            <form method="post" action="/settings/llm/delete" style="display:inline;margin:0" onsubmit="return confirm('Delete {pc.name}?')">
              <input type="hidden" name="name" value="{pc.name}">
              <button class="outline secondary" style="padding:.1rem .5rem;font-size:.7rem;margin:0">Delete</button>
            </form>
          </td>
        </tr>"""
    return f"""
    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Model</th><th></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


@app.get("/settings/llm", response_class=HTMLResponse)
def llm_settings(saved: str = "", edit: str = "") -> HTMLResponse:
    banner = _banner("LLM backend saved.") if saved else ""
    editing = st.load_providers().get(edit) if edit else None
    ptype = editing.type if editing else "cloudflare"

    account = editing.account if editing else ""
    url = editing.url if editing else "http://localhost:11434"
    base_url = editing.base_url if editing else ""
    model = editing.model if editing else ""
    models = editing.models if editing else []
    # Pre-render the saved models as selected options so editing shows the current
    # set before any API fetch; "Load models from API" appends the rest.
    anthropic_model_opts = "".join(
        f'<option value="{m}" selected>{m}</option>' for m in models
    )
    hide_model = ' style="display:none"' if ptype == "anthropic" else ""
    token_ph = "•••• (kept if blank)" if editing and editing.token else "Cloudflare API token"
    key_ph = "•••• (kept if blank)" if editing and editing.api_key else "API key (blank if none)"
    model_ph = CLAUDE_DEFAULT_MODEL if ptype == "anthropic" else CF_DEFAULT_MODEL

    def hide(t: str) -> str:
        return "" if ptype == t else ' style="display:none"'

    def checked(t: str) -> str:
        return "checked" if ptype == t else ""

    if editing:
        name_field = (
            f'<p>Editing <strong>{editing.name}</strong> '
            f'<a href="/settings/llm" style="font-size:.8rem">cancel</a></p>'
            f'<input type="hidden" name="name" value="{editing.name}">'
        )
        form_title = "Edit backend"
        submit_label = "Save changes"
    else:
        name_field = (
            '<label>Name <small class="muted">lowercase, hyphens</small>'
            '<input name="name" placeholder="cf-mistral" required></label>'
        )
        form_title = "Add backend"
        submit_label = "Add backend"

    body = f"""
    {banner}
    <hgroup>
      <h2>LLM backends</h2>
      <p>Inference backends for <code>teasel local</code>. The default is used unless a project's
         <code>teasel.toml</code> names another via <code>[llm] provider</code>.</p>
    </hgroup>
    {_provider_list_html()}

    <article>
    <h3 style="margin-top:0">{form_title}</h3>
    <form method="post" action="/settings/llm/save">
      {name_field}
      <fieldset>
        <legend>Backend type</legend>
        <label><input type="radio" name="type" value="cloudflare" {checked("cloudflare")} onchange="llmType('cloudflare')"> Cloudflare Workers AI</label>
        <label><input type="radio" name="type" value="ollama" {checked("ollama")} onchange="llmType('ollama')"> Ollama / llama-server (local)</label>
        <label><input type="radio" name="type" value="openai-compat" {checked("openai-compat")} onchange="llmType('openai-compat')"> OpenAI-compatible endpoint</label>
        <label><input type="radio" name="type" value="anthropic" {checked("anthropic")} onchange="llmType('anthropic')"> Anthropic (Claude)</label>
      </fieldset>

      <div id="cf-fields"{hide("cloudflare")}>
        <label>Account ID <input name="account" value="{account}" placeholder="Cloudflare account ID"></label>
        <label>API Token <input name="token" value="" placeholder="{token_ph}">
          <small class="muted">Leave blank to keep existing token.</small></label>
      </div>

      <div id="ollama-fields"{hide("ollama")}>
        <label>Ollama URL <input name="url" value="{url}" placeholder="http://localhost:11434"></label>
      </div>

      <div id="oc-fields"{hide("openai-compat")}>
        <label>Base URL <input name="base_url" value="{base_url}" placeholder="https://…/v1"></label>
        <label>API Key <input name="api_key" value="" placeholder="{key_ph}">
          <small class="muted">Leave blank to keep existing / if none.</small></label>
      </div>

      <div id="anthropic-fields"{hide("anthropic")}>
        <label>API Key <input name="anthropic_key" value="" placeholder="{key_ph}">
          <small class="muted">Anthropic API key. Leave blank to keep existing.</small></label>
        <label>Models
          <select name="anthropic_models" id="anthropic-model-list" multiple size="6" style="margin:0">
            {anthropic_model_opts}
          </select>
          <small class="muted">Pick one or more (Ctrl/Cmd-click) — this backend's one key serves them all.
            The first selected is the default. Use “Load models from API” to fetch the catalog.</small></label>
        <div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin:-.2rem 0 .6rem">
          <button type="button" class="outline" style="padding:.2rem .6rem;font-size:.78rem;width:auto;margin:0" onclick="loadAnthropicModels(this)">Load models from API</button>
          <small id="anthropic-model-msg" class="muted" style="font-size:.74rem"></small>
        </div>
      </div>

      <div id="model-field"{hide_model}>
        <label>Model <input name="model" value="{model}" placeholder="{model_ph}"></label>
      </div>
      <button type="submit">{submit_label}</button>
    </form>
    </article>

    <p class="muted" style="margin-top:1rem;font-size:.8rem">
      Credentials are stored in <code>~/.teasel/llm.toml</code> (outside the project directory).
    </p>
    <script>
    function llmType(t){{
      document.getElementById('cf-fields').style.display = t==='cloudflare'?'':'none';
      document.getElementById('ollama-fields').style.display = t==='ollama'?'':'none';
      document.getElementById('oc-fields').style.display = t==='openai-compat'?'':'none';
      document.getElementById('anthropic-fields').style.display = t==='anthropic'?'':'none';
      var mf=document.getElementById('model-field');
      if(mf) mf.style.display = t==='anthropic' ? 'none' : '';
      var mi=document.querySelector('input[name=model]');
      if(mi) mi.placeholder = t==='anthropic' ? '{CLAUDE_DEFAULT_MODEL}' : t==='cloudflare' ? '{CF_DEFAULT_MODEL}' : 'model name';
    }}
    function loadAnthropicModels(btn){{
      var keyEl=document.querySelector('input[name=anthropic_key]');
      var nameEl=document.querySelector('input[name=name]');
      var sel=document.getElementById('anthropic-model-list');
      var msg=document.getElementById('anthropic-model-msg');
      var payload={{ key:(keyEl&&keyEl.value.trim())||'', name:(nameEl&&nameEl.value.trim())||'' }};
      if(!payload.key && !payload.name){{ msg.textContent='enter an API key first'; return; }}
      btn.disabled=true; msg.textContent='loading…';
      fetch('/llm/anthropic/models',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})
        .then(function(r){{return r.json();}})
        .then(function(d){{
          btn.disabled=false;
          if(!d.ok){{ msg.textContent=d.error||'failed'; return; }}
          var have={{}};
          Array.prototype.forEach.call(sel.options,function(o){{ have[o.value]=true; }});
          var added=0;
          d.models.forEach(function(m){{
            if(have[m.id]){{ return; }}
            var o=document.createElement('option'); o.value=m.id;
            o.textContent=(m.display_name||m.id)+' ('+m.id+')';
            sel.appendChild(o); added++;
          }});
          msg.textContent=d.models.length+' available — '+added+' added. Select the ones to enable.';
        }})
        .catch(function(){{ btn.disabled=false; msg.textContent='request failed'; }});
    }}
    </script>
    """
    return _page(body, back="/#settings")


@app.post("/settings/llm/save")
async def llm_settings_save(request: Request) -> RedirectResponse:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    ptype = str(form.get("type", "cloudflare"))
    if not name:
        return RedirectResponse("/settings/llm", status_code=303)

    existing = st.load_providers().get(name)
    pc = st.ProviderConfig(name=name, type=ptype, model=str(form.get("model", "")).strip())

    if ptype == "cloudflare":
        pc.account = str(form.get("account", "")).strip()
        token = str(form.get("token", "")).strip()
        pc.token = token or (existing.token if existing else "")
    elif ptype == "openai-compat":
        pc.base_url = str(form.get("base_url", "")).strip()
        key = str(form.get("api_key", "")).strip()
        pc.api_key = key or (existing.api_key if existing else "")
    elif ptype == "anthropic":
        key = str(form.get("anthropic_key", "")).strip()
        pc.api_key = key or (existing.api_key if existing else "")
        models = [m.strip() for m in form.getlist("anthropic_models") if m.strip()]
        pc.models = models
        # First selected model is the backend's default; keep the old one if none picked.
        pc.model = models[0] if models else (existing.model if existing else "")
    else:  # ollama
        pc.url = str(form.get("url", "")).strip()

    st.save_provider(pc)
    return RedirectResponse("/settings/llm?saved=1", status_code=303)


@app.post("/settings/llm/default")
async def llm_set_default(request: Request) -> RedirectResponse:
    form = await request.form()
    st.set_default_provider(str(form.get("name", "")).strip())
    return RedirectResponse("/settings/llm?saved=1", status_code=303)


@app.post("/settings/llm/delete")
async def llm_delete(request: Request) -> RedirectResponse:
    form = await request.form()
    st.delete_provider(str(form.get("name", "")).strip())
    return RedirectResponse("/settings/llm?saved=1", status_code=303)
