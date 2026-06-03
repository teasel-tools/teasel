"""Local LLM bridge — MCP client + forge agentic loop over an OpenAI-compatible backend."""

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import state as st
from .state import ProviderConfig

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_TEASEL_URL = "http://localhost:7891/mcp"
DEFAULT_OLLAMA_MODEL = "mistral:7b"
CF_DEFAULT_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"
CF_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are a lab instrument assistant. You have access to tools that control real "
    "lab instruments. Use them to carry out the user's requests. Be concise.\n"
    "You can only speak to the user by calling the respond tool. Every reply to the "
    "user must be sent through the respond tool — including greetings, follow-up "
    "questions, and any case where no instrument action is needed. Never reply with "
    "plain text."
)


def connection_from_provider(
    pc: ProviderConfig, model_override: str | None = None
) -> tuple[str, dict, str]:
    """Resolve a provider bundle to (base_url, headers, model) for the forge client.

    ``model_override`` is the ephemeral session-only ``--model`` flag; it wins over
    the bundle's model but is never persisted.
    """
    model = model_override or pc.model
    if pc.type == "cloudflare":
        base_url = CF_BASE_URL.format(account_id=pc.account)
        model = model or CF_DEFAULT_MODEL
        print(f"Using Cloudflare Workers AI ({pc.name}) — {model}")
        return base_url, {"Authorization": f"Bearer {pc.token}"}, model

    if pc.type == "openai-compat":
        headers = {"Authorization": f"Bearer {pc.api_key}"} if pc.api_key else {}
        print(f"Using OpenAI-compatible backend ({pc.name}) at {pc.base_url} — {model}")
        return pc.base_url, headers, model

    # ollama / llama-server
    url = pc.url or DEFAULT_OLLAMA_URL
    model = model or DEFAULT_OLLAMA_MODEL
    print(f"Using Ollama/llama-server ({pc.name}) at {url} — {model}")
    return f"{url}/v1", {}, model


def client_from_provider(pc: ProviderConfig, model_override: str | None = None):
    """Build the forge client for a provider bundle. Returns ``(client, model)``.

    forge's ``WorkflowRunner`` is client-agnostic, so this is the only place that
    knows which concrete client a provider maps to: Anthropic providers use forge's
    native ``AnthropicClient``; everything else (Cloudflare / Ollama / OpenAI-compat)
    goes through ``OpenAICompatClient`` via :func:`connection_from_provider`.
    """
    if pc.type == "anthropic":
        from forge.clients.anthropic import AnthropicClient

        model = model_override or pc.model or CLAUDE_DEFAULT_MODEL
        print(f"Using Anthropic (Claude) ({pc.name}) — {model}")
        return AnthropicClient(model=model, api_key=pc.api_key), model

    from forge.clients.openai_compat import OpenAICompatClient

    base_url, headers, model = connection_from_provider(pc, model_override)
    api_key = headers.get("Authorization", "").removeprefix("Bearer ")
    return OpenAICompatClient(model=model, base_url=base_url, api_key=api_key), model


async def _ainput(prompt: str) -> str:
    """Read a line from stdin off the event loop, via a daemon thread.

    We deliberately don't use ``asyncio.to_thread`` here: its shared
    ThreadPoolExecutor is joined at interpreter exit, and a worker still
    blocked in ``input()`` makes that join hang — so a Ctrl-C at the prompt
    needs several presses and spews a "threading shutdown" traceback. A daemon
    thread is abandoned (killed) at shutdown with no join, so a single Ctrl-C —
    delivered to the main thread, raised out of the ``await`` below — exits
    cleanly while this thread is left to die with the interpreter.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def _read() -> None:
        try:
            text = input(prompt)
            result, exc = text, None
        except BaseException as e:  # EOFError (Ctrl-D) and friends
            result, exc = None, e

        def _deliver() -> None:
            # The future may already be cancelled (Ctrl-C tore down the loop
            # while we were blocked in input()); don't set state on it then.
            if fut.done():
                return
            fut.set_exception(exc) if exc is not None else fut.set_result(result)

        try:
            loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            pass  # event loop already closed during shutdown

    threading.Thread(target=_read, daemon=True).start()
    return await fut


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def run_first_run_wizard() -> ProviderConfig:
    """Prompt for a first LLM backend when none is configured, save it as default."""
    print("\nNo LLM backend configured. Let's set one up.\n")
    print("  1) Cloudflare Workers AI")
    print("  2) Ollama / llama-server (local)")
    print("  3) Other OpenAI-compatible endpoint\n")
    choice = _prompt("Choose a backend (1-3)", "1")

    name = _prompt("Name for this backend (lowercase, hyphens)", "default")

    if choice == "2":
        pc = ProviderConfig(
            name=name,
            type="ollama",
            url=_prompt("Ollama URL", DEFAULT_OLLAMA_URL),
            model=_prompt("Model", DEFAULT_OLLAMA_MODEL),
        )
    elif choice == "3":
        pc = ProviderConfig(
            name=name,
            type="openai-compat",
            base_url=_prompt("Base URL (…/v1)"),
            api_key=_prompt("API key (blank if none)"),
            model=_prompt("Model"),
        )
    else:
        pc = ProviderConfig(
            name=name,
            type="cloudflare",
            account=_prompt("Cloudflare account ID"),
            token=_prompt("Cloudflare API token"),
            model=_prompt("Model", CF_DEFAULT_MODEL),
        )

    st.save_provider(pc, make_default=True)
    print(f"\nSaved backend '{name}' to {st.LLM_CONFIG_PATH} (now the default).\n")
    return pc


def _make_caller(session: ClientSession, tool_name: str):
    """Build an async callable that forwards a forge tool call to the MCP server.

    Raise on an MCP error result (``isError``) instead of returning its text.
    Otherwise a failed call is handed back to the model as if it had succeeded:
    it never rides forge's tool-error channel, never counts toward the error
    budget, and isn't tagged ``[ToolError]``. Raising lets forge treat it as a
    real tool failure — the model is told it failed and can correct.
    """
    async def _call(**kwargs) -> str:
        result = await session.call_tool(tool_name, kwargs)
        text = result.content[0].text if result.content else ""
        if getattr(result, "isError", False):
            raise RuntimeError(text or f"{tool_name} reported an error")
        return text
    return _call


# An event envelope is a plain dict streamed to a consumer (terminal or
# browser). One shape, two sinks — the CLI prints it, the web chat sends it as
# JSON over a WebSocket. Types: "tool_call", "tool_result", "guardrail",
# "text" (final assistant reply), "error", "done".
Emit = Callable[[dict], Awaitable[None]]


def _classify_error(e: Exception) -> str:
    """Frame forge's hard-stop exceptions as a readable one-liner.

    Unlike the soft nudges (which stream through ``on_message`` as guardrail
    events), these mean a budget was exhausted — iteration cap, consecutive
    tool failures, premature-terminal attempts, or prerequisite violations — so
    the runner raised and the turn was abandoned. forge's own messages are
    already descriptive; we just prepend what kind of limit was hit. The local
    import keeps this a graceful ``str(e)`` if the error names ever drift.
    """
    try:
        from forge.errors import (
            MaxIterationsError,
            PrerequisiteError,
            StepEnforcementError,
            ToolCallError,
            ToolExecutionError,
        )
    except Exception:
        return str(e)

    if isinstance(e, MaxIterationsError):
        return f"Hit the {getattr(e, 'iterations', '?')}-step limit before finishing — {e}"
    if isinstance(e, ToolExecutionError):
        return f"A tool failed repeatedly and forge stopped the turn — {e}"
    if isinstance(e, StepEnforcementError):
        return f"Model kept trying to finish before required steps — {e}"
    if isinstance(e, PrerequisiteError):
        return f"Model kept calling a tool with unmet prerequisites — {e}"
    if isinstance(e, ToolCallError):
        return f"Model couldn't produce a usable response after repeated retries — {e}"
    return str(e)


class ChatSession:
    """An open MCP + forge session that runs one user turn at a time, emitting
    structured event envelopes. Shared by the CLI REPL and the web chat — only
    the ``emit`` sink differs.

    Conversation history persists across turns, including the real
    tool-call/result messages — the model needs to *see* that it called tools
    last turn, or it starts answering follow-ups from memory without actually
    driving the instruments. ``on_message`` accumulates every message the runner
    emits; the runner compacts a copy at send time, so holding the full history
    here is fine. A turn ends on the ``respond`` tool, leaving a trailing
    ``tool`` message, and most chat APIs reject a ``user`` message after a
    ``tool`` message — so after each turn we append a synthetic assistant reply,
    keeping a valid role alternation.
    """

    def __init__(self, runner, workflow, history, queue, tool_count: int):
        self._runner = runner
        self._workflow = workflow
        self.history = history
        self._queue = queue
        self.tool_count = tool_count

    async def run_turn(self, user_input: str, emit: Emit) -> None:
        from forge.core.messages import Message, MessageMeta, MessageRole, MessageType

        if not self.history:
            self.history.append(Message(
                MessageRole.SYSTEM, SYSTEM_PROMPT,
                MessageMeta(MessageType.SYSTEM_PROMPT),
            ))

        # Checkpoint so a failed turn rolls back to a clean state (the runner
        # appends partial messages via on_message during the run).
        checkpoint = len(self.history)
        self.history.append(Message(
            MessageRole.USER, user_input,
            MessageMeta(MessageType.USER_INPUT),
        ))

        # Drain the event queue concurrently so tool traces stream live while
        # the runner works, then flush anything left before the final reply.
        drainer = asyncio.create_task(self._drain(emit))
        try:
            result = await self._runner.run(
                self._workflow, user_input, initial_messages=self.history
            )
            result_str = result if isinstance(result, str) else str(result)
            # End the turn on an assistant message so the next user message
            # follows a valid role (not a trailing tool result).
            self.history.append(Message(
                MessageRole.ASSISTANT, result_str,
                MessageMeta(MessageType.TEXT_RESPONSE),
            ))
            await self._stop_drain(drainer)
            await emit({"type": "text", "text": result_str})
        except Exception as e:
            del self.history[checkpoint:]
            await self._stop_drain(drainer)
            await emit({"type": "error", "text": _classify_error(e)})
        finally:
            await emit({"type": "done"})

    async def _drain(self, emit: Emit) -> None:
        while True:
            ev = await self._queue.get()
            if ev is None:  # sentinel — turn's runner has finished
                return
            await emit(ev)

    async def _stop_drain(self, drainer: asyncio.Task) -> None:
        self._queue.put_nowait(None)
        await drainer


@asynccontextmanager
async def chat_session(client, teasel_url: str):
    """Open an MCP connection + forge runner, yielding a :class:`ChatSession`.

    ``client`` is a pre-built forge client (see :func:`client_from_provider`) — this
    function is client-agnostic. The MCP connection stays open for the lifetime of
    the context, so one session spans a whole conversation (CLI REPL or one browser
    WebSocket).
    """
    from forge import ToolDef, ToolSpec, Workflow, WorkflowRunner
    from forge.context import ContextManager, TieredCompact
    from forge.core.messages import Message, MessageRole, MessageType
    from forge.tools import RESPOND_TOOL_NAME, respond_tool

    history: list[Message] = []
    queue: asyncio.Queue = asyncio.Queue()

    guardrail_labels = {
        MessageType.RETRY_NUDGE: "retry nudge",
        MessageType.STEP_NUDGE: "step nudge",
        MessageType.PREREQUISITE_NUDGE: "prerequisite nudge",
        MessageType.CONTEXT_WARNING: "context warning",
    }

    def _on_message(msg: Message) -> None:
        history.append(msg)
        mtype = msg.metadata.type
        content = msg.content or ""

        # Corrective interventions first. Several of forge's nudges (unknown
        # tool, premature step, prerequisite) arrive as TOOL-role messages, so
        # they must be classified here — before the generic tool-result branch
        # below, which would otherwise log them as ordinary results.
        if mtype in guardrail_labels:
            label = guardrail_labels[mtype]
            # RETRY_NUDGE covers two distinct failures — say which. Unknown-tool
            # rides a TOOL-role "[UnknownTool] ..." message; a bare-text reply
            # rides a USER-role nudge.
            if mtype == MessageType.RETRY_NUDGE:
                label = ("unknown tool" if content.startswith("[UnknownTool]")
                         else "invalid response (not a tool call)")
            tier = getattr(msg.metadata, "nudge_tier", 0)
            if tier > 1:
                label = f"{label} · escalated to tier {tier}"
            queue.put_nowait({"type": "guardrail", "label": label, "text": content})
            return

        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            # Rescue: forge silently parsed these calls out of a bare-text reply.
            # Emit before the call so the trace reads fumble → rescue → call.
            # getattr keeps this a no-op on a forge build without the metadata.
            if getattr(msg.metadata, "rescued", False):
                original = (getattr(msg.metadata, "rescued_from", "") or "").strip()
                text = "model replied with unstructured text; forge parsed a valid tool call from it"
                if original:
                    snippet = original if len(original) <= 200 else original[:197] + "..."
                    text += f" — original: {snippet}"
                queue.put_nowait({"type": "guardrail", "label": "rescued tool call",
                                  "text": text})
            calls = [
                {"name": tc.name, "args": tc.args}
                for tc in msg.tool_calls
                if tc.name != RESPOND_TOOL_NAME
            ]
            if calls:
                queue.put_nowait({"type": "tool_call", "calls": calls})
        elif msg.role == MessageRole.TOOL and msg.tool_name != RESPOND_TOOL_NAME:
            # A tool that raised surfaces as a TOOL_RESULT with an error prefix;
            # flag it as a guardrail event rather than a plain result.
            if content.startswith(("[ToolError]", "[ToolResolutionError]")):
                queue.put_nowait({"type": "guardrail", "label": "tool error",
                                  "name": msg.tool_name, "text": content})
            else:
                queue.put_nowait({"type": "tool_result", "name": msg.tool_name,
                                  "text": content})

    runner = WorkflowRunner(
        client=client,
        context_manager=ContextManager(
            strategy=TieredCompact(keep_recent=4),
            budget_tokens=16_000,
        ),
        # Each tool call is one iteration; a manual instrument sweep is ~2 calls
        # per point (set + measure), so keep this generous enough for a demo
        # sweep while still bounding a runaway loop.
        max_iterations=40,
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
                # System prompt is seeded directly into history on the first
                # turn, so forge's template path is never invoked.
                system_prompt_template="",
            )

            yield ChatSession(runner, workflow, history, queue, len(mcp_tools))


def _print_envelope(ev: dict) -> None:
    """Render an event envelope to the terminal (the CLI's emit sink)."""
    t = ev["type"]
    if t == "tool_call":
        for c in ev["calls"]:
            print(f"  → {c['name']}({c['args']})")
    elif t == "tool_result":
        first_line = (ev["text"] or "").split("\n", 1)[0]
        if len(first_line) > 120:
            first_line = first_line[:117] + "..."
        print(f"  ← {first_line}")
    elif t == "guardrail":
        first_line = (ev["text"] or "").split("\n", 1)[0]
        print(f"  ⚠ guardrail [{ev['label']}]: {first_line[:120]}")
    elif t == "text":
        print(ev["text"])
    elif t == "error":
        print(f"[error] {ev['text']}")
    # "done" needs no terminal output


async def run(pc: ProviderConfig, teasel_url: str, model_override: str | None = None) -> None:
    async def emit(ev: dict) -> None:
        _print_envelope(ev)

    client, _model = client_from_provider(pc, model_override)
    async with chat_session(client, teasel_url) as session:
        print(f"Connected — {session.tool_count} tools available.")
        print("Type your request (Ctrl+D to quit).\n")

        while True:
            try:
                user_input = (await _ainput("> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            await session.run_turn(user_input, emit)
