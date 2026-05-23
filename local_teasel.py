"""Teasel local LLM bridge — thin wrapper; use 'teasel local' or run directly."""

import asyncio
import argparse
import os

from teasel import state as st
from teasel.local_llm import build_base_url_and_headers, run


def main() -> None:
    parser = argparse.ArgumentParser(description="Teasel local LLM bridge")
    parser.add_argument("--llama-url", default="http://localhost:11434",
                        help="Ollama/llama-server base URL (default: %(default)s)")
    parser.add_argument("--teasel-url", default="http://localhost:7891/mcp",
                        help="teasel-server MCP URL (default: %(default)s)")
    parser.add_argument("--model", default=None,
                        help="model name to use (default depends on backend)")
    parser.add_argument("--cf-account", default=os.environ.get("CF_ACCOUNT_ID"),
                        help="Cloudflare account ID (or set CF_ACCOUNT_ID env var)")
    parser.add_argument("--cf-token", default=os.environ.get("CF_API_TOKEN"),
                        help="Cloudflare API token (or set CF_API_TOKEN env var)")
    args = parser.parse_args()

    saved_cfg = st.load_llm_config()
    base_url, headers, model = build_base_url_and_headers(
        saved_cfg, args.cf_account, args.cf_token, args.llama_url, args.model
    )
    asyncio.run(run(base_url, args.teasel_url, model, headers))


if __name__ == "__main__":
    main()
