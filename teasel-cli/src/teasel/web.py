from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import state as st

app = FastAPI(title="teasel", version="0.1")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
    <html>
      <head><title>teasel</title></head>
      <body>
        <h1>teasel</h1>
        <p>Web UI coming soon. Try <a href="/api/instruments">/api/instruments</a>.</p>
      </body>
    </html>
    """


@app.get("/api/instruments")
def list_instruments() -> list[dict]:
    return [
        {"slug": i.slug, "package": i.package, "env": i.env}
        for i in st.load()
    ]
