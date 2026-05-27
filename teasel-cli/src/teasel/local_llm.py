"""Local LLM bridge — MCP client + forge agentic loop over an OpenAI-compatible backend."""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_TEASEL_URL = "http://localhost:7891/mcp"
DEFAULT_OLLAMA_MODEL = "mistral:7b"
CF_DEFAULT_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"

SYSTEM_PROMPT = (
    "You are a lab instrument assistant. You have access to tools that control real "
    "lab instruments. Use them to answer the user's requests. Be concise. "
    "When you have finished, call the respond tool to reply to the user."
)


def build_base_url_and_headers(
    saved_cfg: dict,
    cf_account: str | None,
    cf_token: str | None,
    ollama_url: str,
    model_override: str | None,
) -> tuple[str, dict, str]:
    """Return (base_url, headers, model). CLI flags > saved config > defaults."""
    if cf_account and cf_token:
        base_url = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1"
        model = model_override or CF_DEFAULT_MODEL
        print(f"Using Cloudflare Workers AI — {model}")
        return base_url, {"Authorization": f"Bearer {cf_token}"}, model

    if saved_cfg.get("provider") == "cloudflare":
        account = saved_cfg.get("cf_account", "")
        token = saved_cfg.get("cf_token", "")
        if account and token:
            base_url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1"
            model = model_override or saved_cfg.get("model", CF_DEFAULT_MODEL)
            print(f"Using Cloudflare Workers AI — {model}")
            return base_url, {"Authorization": f"Bearer {token}"}, model

    url = saved_cfg.get("ollama_url", ollama_url)
    model = model_override or saved_cfg.get("model", DEFAULT_OLLAMA_MODEL)
    print(f"Using Ollama/llama-server at {url} — {model}")
    return f"{url}/v1", {}, model


def _make_caller(session: ClientSession, tool_name: str):
    """Build an async callable that forwards a forge tool call to the MCP server."""
    async def _call(**kwargs) -> str:
        result = await session.call_tool(tool_name, kwargs)
        return result.content[0].text if result.content else ""
    return _call


async def run(base_url: str, teasel_url: str, model: str, headers: dict) -> None:
    from forge import ToolDef, ToolSpec, Workflow, WorkflowRunner
    from forge.clients.openai_compat import OpenAICompatClient
    from forge.context import ContextManager, TieredCompact
    from forge.core.messages import Message, MessageMeta, MessageRole, MessageType
    from forge.tools import RESPOND_TOOL_NAME, respond_tool

    api_key = headers.get("Authorization", "").removeprefix("Bearer ")
    client = OpenAICompatClient(model=model, base_url=base_url, api_key=api_key)

    # Full conversation history persisted across REPL turns, including the
    # real tool-call/result messages — the model needs to *see* that it called
    # tools last turn, or it starts answering follow-ups from memory without
    # actually driving the instruments. on_message accumulates every message
    # the runner emits. The runner compacts a copy at send time, so holding the
    # full history here is fine.
    #
    # A turn ends on the `respond` tool, leaving a trailing `tool` message, and
    # most chat APIs reject a `user` message after a `tool` message. So after
    # each turn we append a synthetic assistant reply (the respond text), making
    # history end on `assistant` and keeping a valid role alternation.
    history: list[Message] = []

    def _on_message(msg: Message) -> None:
        history.append(msg)
        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.name != RESPOND_TOOL_NAME:
                    print(f"  → {tc.name}({tc.args})")

    runner = WorkflowRunner(
        client=client,
        context_manager=ContextManager(
            strategy=TieredCompact(keep_recent=4),
            budget_tokens=16_000,
        ),
        max_iterations=15,
        on_message=_on_message,
    )

    async with streamablehttp_client(teasel_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            tools: dict[str, ToolDef] = {}
            for t in mcp_tools:
                spec = ToolSpec.from_json_schema(t.name, t.description or "", t.inputSchema)
                tools[t.name] = ToolDef(
                    spec=spec, callable=_make_caller(session, t.name), prerequisites=[]
                )

            tools[RESPOND_TOOL_NAME] = respond_tool()

            workflow = Workflow(
                name="teasel_lab",
                description="Control lab instruments",
                tools=tools,
                required_steps=[],
                terminal_tool=RESPOND_TOOL_NAME,
                # System prompt is seeded directly into `initial_messages` below,
                # so forge's template path is never invoked.
                system_prompt_template="",
            )

            print(f"Connected — {len(mcp_tools)} tools available.")
            print("Type your request (Ctrl+C to quit).\n")

            while True:
                try:
                    user_input = await asyncio.to_thread(input, "> ")
                    user_input = user_input.strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not user_input:
                    continue

                if not history:
                    history.append(Message(
                        MessageRole.SYSTEM, SYSTEM_PROMPT,
                        MessageMeta(MessageType.SYSTEM_PROMPT),
                    ))

                # Checkpoint so a failed turn rolls back to a clean state
                # (the runner may have appended partial messages via on_message).
                checkpoint = len(history)
                history.append(Message(
                    MessageRole.USER, user_input,
                    MessageMeta(MessageType.USER_INPUT),
                ))

                try:
                    result = await runner.run(
                        workflow, user_input, initial_messages=history
                    )
                    result_str = result if isinstance(result, str) else str(result)
                    print(result_str)
                    # End the turn on an assistant message so the next user
                    # message follows a valid role (not a trailing tool result).
                    history.append(Message(
                        MessageRole.ASSISTANT, result_str,
                        MessageMeta(MessageType.TEXT_RESPONSE),
                    ))
                except Exception as e:
                    del history[checkpoint:]
                    print(f"[error] {e}")
