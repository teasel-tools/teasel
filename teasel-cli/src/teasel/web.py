"""Web UI for teasel — FastAPI + htmx + Pico CSS."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config as cfg
from . import get_version
from . import state as st
from .registry import DriverDescriptor, fetch_driver, fetch_index

app = FastAPI(title="teasel", version=get_version())


# ── Template helpers ──────────────────────────────────────────────────────────

_MOON = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
_SUN  = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'


def _page(body: str, back: str | None = None) -> HTMLResponse:
    ver = get_version()
    back_html = f'<li><a href="{back}" style="font-size:.9em">← Back</a></li>' if back else ""
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
    #theme-toggle {{ background: none; border: none; cursor: pointer; padding: .25rem; color: var(--pico-muted-color); line-height: 1; vertical-align: middle; }}
    #theme-toggle:hover {{ color: var(--pico-color); }}
    [data-theme="dark"]  #icon-moon {{ display: none; }}
    [data-theme="light"] #icon-sun  {{ display: none; }}
    .muted {{ color: var(--pico-muted-color); font-size: .9em; }}
    mark.green {{ background: none; color: var(--pico-color-jade-550); }}
    table tr.link {{ cursor: pointer; }}
    fieldset {{ border: 1px solid var(--pico-table-border-color); border-radius: var(--pico-border-radius); padding: 1rem; margin-bottom: 1rem; }}
    fieldset legend {{ padding: 0 .5rem; font-weight: bold; }}
  </style>
</head>
<body hx-boost="true">
  <header>
    <nav>
      <ul><li><a href="/"><strong>teasel</strong></a></li></ul>
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
    instruments = st.load()
    setups = {s.slug: s for s in st.load_setup()}

    banner = _banner("Configuration saved — Claude will pick it up automatically.") if saved else ""

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

    body = f"""
    {banner}
    <hgroup>
      <h2>Connected Instruments</h2>
      <p>Instruments in <code>teasel.toml</code>, available to Claude via MCP.</p>
    </hgroup>
    {content}
    <a href="/registry" role="button" class="outline">⊕ Add device</a>
    """
    return _page(body)


@app.get("/instrument/{slug}", response_class=HTMLResponse)
def instrument_detail(slug: str, saved: str = "") -> HTMLResponse:
    instruments = st.load()
    inst = next((i for i in instruments if i.slug == slug), None)
    if inst is None:
        return _page(f'<p>Instrument <code>{slug}</code> not found.</p>', back="/")

    setup = next((s for s in st.load_setup() if s.slug == slug), st.InstrumentSetup(slug=slug))
    banner = _banner("Saved.") if saved else ""

    conn_rows = "".join(
        f"<tr><td class='muted'>{k}</td><td><code>{v}</code></td></tr>"
        for k, v in inst.params.items()
    ) or "<tr><td colspan='2'><em class='muted'>No connection params</em></td></tr>"

    setup_rows = ""
    for k, v in setup.limits.items():
        setup_rows += f"<tr><td class='muted'>{k}</td><td>{v}</td></tr>"
    for ch, ch_cfg in setup.channels.items():
        for field, val in ch_cfg.items():
            setup_rows += f"<tr><td class='muted'>{ch}.{field}</td><td>{val}</td></tr>"
    if not setup_rows:
        setup_rows = "<tr><td colspan='2'><em class='muted'>Not configured</em></td></tr>"

    body = f"""
    {banner}
    <hgroup>
      <h2>{slug}</h2>
      <p class="muted">{inst.type or "instrument"} &ensp;·&ensp; {inst.package}</p>
    </hgroup>

    <div style="display:flex; align-items:baseline; gap:1rem; margin-bottom:.5rem">
      <h3 style="margin:0">Connection</h3>
      <a href="/instrument/{slug}/connection">Edit</a>
    </div>
    <table><tbody>{conn_rows}</tbody></table>

    <div style="display:flex; align-items:baseline; gap:1rem; margin-bottom:.5rem; margin-top:1.5rem">
      <h3 style="margin:0">Experiment setup</h3>
      <a href="/instrument/{slug}/setup">Edit</a>
    </div>
    <table><tbody>{setup_rows}</tbody></table>

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
      <p>Updates <code>setup.toml</code></p>
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


@app.post("/instrument/{slug}/delete")
def instrument_delete(slug: str) -> RedirectResponse:
    instruments = [i for i in st.load() if i.slug != slug]
    st.save(instruments)
    cfg.apply(instruments)
    return RedirectResponse("/", status_code=303)


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
      <p>Step 2 of 2 — experiment settings → <code>setup.toml</code>
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


def _setup_fields(inst: st.InstrumentConfig, current: st.InstrumentSetup) -> str:
    html = ""
    if inst.type == "function-generator":
        amp = current.limits.get("amplitude_max", "")
        freq = current.limits.get("frequency_max", "")
        html += f"""
        <label>
          Amplitude limit (Vpp)
          <small class="muted">leave blank for no limit</small>
          <input name="limit_amplitude_max" type="number" step="any" value="{amp}" placeholder="no limit">
        </label>
        <label>
          Frequency limit (Hz)
          <small class="muted">leave blank for no limit</small>
          <input name="limit_frequency_max" type="number" step="any" value="{freq}" placeholder="no limit">
        </label>"""
    if inst.type == "oscilloscope":
        for ch in ("C1", "C2", "C3", "C4"):
            ch_cfg = current.channels.get(ch, {})
            probe = ch_cfg.get("probe", "")
            label = ch_cfg.get("label", "")
            html += f"""
            <fieldset>
              <legend>{ch}</legend>
              <div style="display:grid; grid-template-columns:1fr 2fr; gap:1rem">
                <label>Probe ratio
                  <input name="ch_{ch}_probe" value="{probe}" placeholder="e.g. 10x">
                </label>
                <label>Label
                  <input name="ch_{ch}_label" value="{label}" placeholder="optional">
                </label>
              </div>
            </fieldset>"""
    return html


def _save_setup(inst: st.InstrumentConfig, form) -> None:
    limits: dict[str, float] = {}
    channels: dict[str, dict] = {}
    for key in ("amplitude_max", "frequency_max"):
        val = str(form.get(f"limit_{key}", "")).strip()
        if val:
            try:
                limits[key] = float(val)
            except ValueError:
                pass
    for ch in ("C1", "C2", "C3", "C4"):
        probe = str(form.get(f"ch_{ch}_probe", "")).strip()
        label = str(form.get(f"ch_{ch}_label", "")).strip()
        ch_data: dict = {}
        if probe:
            ch_data["probe"] = probe
        if label:
            ch_data["label"] = label
        if ch_data:
            channels[ch] = ch_data
    existing = [s for s in st.load_setup() if s.slug != inst.slug]
    existing.append(st.InstrumentSetup(slug=inst.slug, limits=limits, channels=channels))
    st.save_setup(existing)
