"""Web UI for teasel — FastAPI + htmx + Pico CSS."""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    var tabs=['instruments','board','chat','settings'];
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

    return f"""
    {banner}
    <hgroup>
      <h2>Settings</h2>
      <p class="muted">How teasel is wired up — inference, simulation, and the Claude MCP bridge.</p>
    </hgroup>
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
<hgroup>
  <h2>Lab chat</h2>
  <p class="muted" id="chat-status">connecting…</p>
</hgroup>
<p id="chat-probe" class="muted" style="margin:-.4rem 0 .5rem;font-size:.82rem"></p>
<style>
  #chat-log{height:55vh;overflow-y:auto;border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius);padding:.75rem;display:flex;flex-direction:column;gap:.5rem;margin-bottom:.75rem;background:var(--pico-card-background-color)}
  #chat-log .msg{max-width:85%;padding:.4rem .65rem;border-radius:10px;font-size:.9rem;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}
  #chat-log .user{align-self:flex-end;background:var(--pico-primary-background);color:var(--pico-primary-inverse);border-bottom-right-radius:3px}
  #chat-log .assistant{align-self:flex-start;background:var(--pico-code-background-color);border-bottom-left-radius:3px}
  #chat-log .trace{align-self:flex-start;font-family:ui-monospace,monospace;font-size:.72rem;color:var(--pico-muted-color);white-space:pre-wrap;word-break:break-all;max-width:100%}
  #chat-log .guardrail{align-self:flex-start;font-size:.75rem;color:var(--pico-color-amber-550)}
  #chat-log .error{align-self:flex-start;font-size:.82rem;color:var(--pico-color-red-550)}
  #chat-log .empty{margin:auto;color:var(--pico-muted-color);font-size:.85rem;text-align:center}
  #chat-log .artifact{align-self:flex-start;max-width:92%}
  #chat-log .artifact img{display:block;max-width:100%;border-radius:10px;border:1px solid var(--pico-table-border-color)}
</style>
<div id="chat-log"><div class="empty">Ask teasel to read or drive a connected instrument.</div></div>
<form id="chat-form" autocomplete="off" hx-boost="false">
  <fieldset role="group" style="margin:0">
    <input id="chat-input" name="msg" placeholder="e.g. set the function generator to 10 kHz" disabled>
    <button id="chat-send" type="submit" disabled style="width:auto">Send</button>
  </fieldset>
</form>
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
  // Tool results that saved a plot or screenshot get the image inlined.
  function addArtifacts(text){
    var re=/\b(plot_[A-Za-z0-9_]+\.svg|screenshot_[A-Za-z0-9._-]+\.png)\b/g, m, seen={};
    while((m=re.exec(text||''))!==null){
      if(seen[m[1]]){continue;} seen[m[1]]=true;
      var d=document.createElement('div'); d.className='artifact';
      var img=document.createElement('img'); img.src='/artifacts/'+m[1]; img.alt=m[1];
      img.onload=function(){ log.scrollTop=log.scrollHeight; };
      img.onerror=function(){ d.remove(); };
      d.appendChild(img); log.appendChild(d); log.scrollTop=log.scrollHeight;
    }
  }
  var proto=location.protocol==='https:'?'wss:':'ws:';
  var ws=new WebSocket(proto+'//'+location.host+'/ws/chat?brain=local');
  ws.onclose=function(){ busy(true); statusEl.textContent='disconnected — reload to reconnect'; };
  ws.onerror=function(){ statusEl.textContent='connection error'; };
  ws.onmessage=function(e){
    var m=JSON.parse(e.data);
    if(m.type==='ready'){ statusEl.textContent=m.provider+' · '+m.model+' · '+m.tools+' tools'; busy(false); }
    else if(m.type==='text'){ add('msg assistant', m.text); }
    else if(m.type==='tool_call'){ m.calls.forEach(function(c){ add('trace','→ '+c.name+'('+JSON.stringify(c.args)+')'); }); }
    else if(m.type==='tool_result'){ add('trace','← '+(m.text||'').split('\n')[0].slice(0,200)); addArtifacts(m.text); }
    else if(m.type==='guardrail'){ add('guardrail','⚠ '+m.label+': '+(m.text||'').split('\n')[0].slice(0,200)); }
    else if(m.type==='error'){ add('error', m.text); busy(false); }
    else if(m.type==='done'){ busy(false); }
  };
  form.addEventListener('submit',function(ev){
    ev.preventDefault();
    var text=input.value.trim();
    if(!text||ws.readyState!==1){ return; }
    add('msg user', text);
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

    pc = st.resolve_provider(None, Path.cwd())
    if pc is None:
        await websocket.send_json({
            "type": "error",
            "text": "No LLM backend configured. Add one under LLM settings, then reload.",
        })
        await websocket.close()
        return

    client, model = client_from_provider(pc)
    try:
        async with chat_session(client, DEFAULT_TEASEL_URL) as session:
            await websocket.send_json({
                "type": "ready",
                "provider": pc.name,
                "model": model,
                "tools": session.tool_count,
            })
            while True:
                user_input = (await websocket.receive_text()).strip()
                if user_input:
                    await session.run_turn(_with_probes(user_input), websocket.send_json)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Most likely teasel-server isn't running — surface it instead of a
        # silent drop. Best-effort; the socket may already be gone.
        try:
            await websocket.send_json({
                "type": "error",
                "text": f"Could not reach teasel-server at {DEFAULT_TEASEL_URL} ({exc}). "
                        "Start it with: teasel-server --transport streamable-http",
            })
            await websocket.close()
        except Exception:
            pass


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
        amp = current.limits.get("amplitude_max", "")
        freq = current.limits.get("frequency_max", "")
        fg_rows = ""
        for ch, ch_label in (("output", "Main"), ("ttl", "TTL")):
            lbl = current.channels.get(ch, {}).get("label", "")
            fg_rows += f"""
          <div class="ch-name">{ch_label}</div>
          <input name="ch_{ch}_label" value="{lbl}" placeholder="—"{list_attr}>
          <button type="button" class="clear-x" title="Clear"
                  onclick="document.querySelector('[name=ch_{ch}_label]').value=''">×</button>"""
        html += f"""
        <div class="fg-grid">
          <div class="ch-hdr">Output</div>
          <div class="ch-hdr">Label</div>
          <div></div>
          {fg_rows}
        </div>
        <div class="limit-row">
          <label>Amplitude limit (Vpp)
            <input name="limit_amplitude_max" type="number" step="any" value="{amp}" placeholder="no limit">
          </label>
          <label>Frequency limit (Hz)
            <input name="limit_frequency_max" type="number" step="any" value="{freq}" placeholder="no limit">
          </label>
        </div>"""
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
    limits: dict[str, float] = {}
    for key in ("amplitude_max", "frequency_max"):
        val = str(form.get(f"limit_{key}", "")).strip()
        if val:
            try:
                limits[key] = float(val)
            except ValueError:
                pass

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
      </div>

      <label>Model <input name="model" value="{model}" placeholder="{model_ph}"></label>
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
      var mi=document.querySelector('input[name=model]');
      if(mi) mi.placeholder = t==='anthropic' ? '{CLAUDE_DEFAULT_MODEL}' : t==='cloudflare' ? '{CF_DEFAULT_MODEL}' : 'model name';
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
