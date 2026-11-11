"""Jarvis's brains. Pick the default with BRAIN= in .env:

  ollama      free local model on your GPU (default)
  claude      your Claude Pro plan, through Claude Code
  codex       your ChatGPT plan, through Codex
  claude-api  Anthropic API key (pay per use)
  openai      OpenAI API key (pay per use)

All of them get the same tools from tools.py.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tools import TOOLS, load_memory, run_tool

HERE = Path(__file__).parent

SYSTEM_PROMPT = """You are Jarvis, a personal assistant with a dry British wit, talking out loud
to the user through speakers. Sound like a real person: warm, relaxed, natural contractions,
the occasional light quip. Keep it short, usually one or two sentences, because everything you
say is spoken. Never use markdown, lists, emoji or URLs in what you say.

Use your tools whenever they help: do the thing rather than describing how to do it.
After a tool runs, briefly confirm what happened in plain speech. If something isn't possible
with your tools, say so honestly and suggest the closest thing you can do.
When the user tells you something worth keeping (their name, likes, routines), save it with remember.
Learn as you go: if you can't tell which app the user means, ask, then save their answer with
learn_nickname so you never have to ask again. If they open the same few apps together, offer to make
a routine for it. You can open any installed app or Steam game by name.
Never close lots of windows at once. If the user asks to close all or most of their windows, don't do it:
ask which apps they want closed. Close at most what they name.

Things you remember about the user:
{memory}"""

MAX_TOOL_ROUNDS = 8


def _system() -> str:
    facts = load_memory()
    name = os.getenv("JARVIS_USER_NAME", "").strip()
    who = f"\nThe user's name is {name}. Use it now and then, naturally.\n" if name else ""
    return SYSTEM_PROMPT.format(memory="\n".join(f"- {f}" for f in facts) or "(nothing yet)") + who


def _clean(text: str) -> str:
    """Drop any <think> blocks and markdown symbols that shouldn't be read aloud."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return re.sub(r"[*#`_]", "", text).strip()


# ------------------------------------------------- OpenAI-style (incl. Ollama) --
class OpenAIBrain:
    label = "ChatGPT API"

    def __init__(self, model=None, base_url=None, api_key=None, extra_system="", **request_options):
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key) if base_url else OpenAI()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5")
        self.extra_system = extra_system
        self.request_options = request_options
        self.history: list = [{"role": "system", "content": _system()}]
        self.tools = [{"type": "function", "function": {"name": f.__name__, "description": d, "parameters": s}}
                      for f, d, s in TOOLS]

    def ask(self, text: str) -> str:
        self.history[0] = {"role": "system", "content": _system() + self.extra_system}  # refresh memory
        self.history.append({"role": "user", "content": text})
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.chat.completions.create(
                model=self.model, messages=self.history, tools=self.tools, **self.request_options
            )
            msg = response.choices[0].message
            self.history.append(msg)
            if not msg.tool_calls:
                return _clean(msg.content)
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.history.append({"role": "tool", "tool_call_id": call.id,
                                     "content": run_tool(call.function.name, args)})
        return "That took more steps than I expected. Let's try it another way."


class OllamaBrain(OpenAIBrain):
    label = "Ollama"

    def __init__(self):
        super().__init__(
            model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
            base_url=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            reasoning_effort="none",  # skip the model's slow hidden "thinking"
        )
        # Load the model onto the GPU in the background so the first reply is quick.
        import threading

        threading.Thread(target=self._warm_up, daemon=True).start()

    def _warm_up(self):
        try:
            self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": "hi"}],
                max_tokens=1, reasoning_effort="none")
        except Exception:
            pass  # Ollama not running; the real request will report it


# ------------------------------------------------------------- Claude API --
class ClaudeAPIBrain:
    label = "Claude API"

    def __init__(self):
        import anthropic

        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
        self.history: list[dict] = []
        self.tools = [{"name": f.__name__, "description": d, "input_schema": s} for f, d, s in TOOLS]
        self.tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 3})

    def ask(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        start = len(self.history)
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=_system(),
                messages=self.history,
                tools=self.tools,
                output_config={"effort": os.getenv("CLAUDE_EFFORT", "low")},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                del self.history[start - 1:]
                return "Sorry, I can't help with that one."
            self.history.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                return _clean("".join(b.text for b in response.content if b.type == "text"))
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, dict(b.input))}
                for b in response.content if b.type == "tool_use"
            ]
            self.history.append({"role": "user", "content": results})
        return "That took more steps than I expected. Let's try it another way."


# ------------------------------------------ Subscriptions via the CLIs --
NO_WINDOW = 0x08000000  # don't flash a console window when running in the background


def _console_python() -> str:
    """The tool server talks over stdin/stdout, which pythonw (no console) doesn't have.
    So even when Jarvis itself runs under pythonw, start the tool server with python.exe."""
    exe = Path(sys.executable)
    if exe.stem.lower().startswith("pythonw"):
        candidate = exe.with_name(exe.name.lower().replace("pythonw", "python"))
        if candidate.exists():
            return str(candidate)
        found = shutil.which("python")
        if found:
            return found
    return str(exe)


def _env_without(*keys: str) -> dict:
    """So the CLIs use your subscription login, not an API key from .env."""
    return {k: v for k, v in os.environ.items() if k not in keys}


class ClaudeCodeBrain:
    """Uses your Claude Pro/Max plan through the `claude` command.

    Locked down: no built-in tools except web search, only Jarvis's MCP tools,
    and no project or user settings are loaded.
    """

    label = "Claude (Pro plan)"

    def __init__(self):
        self.exe = shutil.which("claude") or "claude"
        self.model = os.getenv("CLAUDE_CODE_MODEL", "sonnet")
        self.session_id = None
        self.mcp_config = json.dumps({"mcpServers": {"jarvis": {
            "command": _console_python(), "args": [str(HERE / "mcp_server.py")]}}})

    def ask(self, text: str) -> str:
        cmd = [self.exe, "-p", "--output-format", "json", "--model", self.model,
               "--mcp-config", self.mcp_config, "--strict-mcp-config",
               "--tools", "WebSearch", "--allowedTools", "mcp__jarvis", "WebSearch",
               "--setting-sources", "", "--system-prompt", _system()]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        out = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW,
                             cwd=HERE, env=_env_without("ANTHROPIC_API_KEY"), timeout=180)
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Claude Code failed: {(out.stderr or out.stdout).strip()[:300]}")
        self.session_id = data.get("session_id", self.session_id)
        if data.get("is_error"):
            raise RuntimeError(f"Claude Code error: {data.get('result')}")
        return _clean(data.get("result", ""))


class CodexBrain:
    """Uses your ChatGPT plan (Go, Plus, Pro...) through the `codex` command.

    Runs read-only with no approval prompts, and with Jarvis's MCP tools added.
    """

    label = "ChatGPT (Codex)"

    def __init__(self):
        self.exe = shutil.which("codex") or "codex"
        self.model = os.getenv("CODEX_MODEL", "")
        self.thread_id = None
        py = _console_python().replace("\\", "/")
        server = str(HERE / "mcp_server.py").replace("\\", "/")
        self.config = [
            "-c", 'approval_policy="never"',
            "-c", 'model_reasoning_effort="low"',
            "-c", f"mcp_servers.jarvis.command='{py}'",
            "-c", f"mcp_servers.jarvis.args=['{server}']",
            # Jarvis's own tools are pre-approved; everything else stays read-only.
            "-c", 'mcp_servers.jarvis.default_tools_approval_mode="approve"',
        ]

    def ask(self, text: str) -> str:
        opts = ["--json", "--skip-git-repo-check", "--sandbox", "read-only", *self.config]
        if self.model:
            opts += ["-m", self.model]
        if self.thread_id:
            cmd = [self.exe, "exec", *opts, "resume", self.thread_id, "-"]
            prompt = text
        else:
            cmd = [self.exe, "exec", *opts, "-"]
            prompt = f"<instructions>\n{_system()}\n</instructions>\n\n{text}"
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW,
                             cwd=HERE, env=_env_without("OPENAI_API_KEY"), timeout=240)
        reply, error = "", ""
        for line in out.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "thread.started":
                self.thread_id = ev.get("thread_id", self.thread_id)
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                reply = item.get("text", "")
            if ev.get("type") in ("error", "turn.failed"):
                error = str(ev.get("message") or ev.get("error"))
        if not reply:
            raise RuntimeError(f"Codex failed: {error or (out.stderr or '').strip()[-300:]}")
        return _clean(reply)


# ------------------------------------------------------------------ pick --
BRAINS = {
    "ollama": OllamaBrain,
    "claude": ClaudeCodeBrain,
    "codex": CodexBrain,
    "chatgpt": CodexBrain,
    "claude-api": ClaudeAPIBrain,
    "openai": OpenAIBrain,
}

# Say "ask Claude ..." or "ask ChatGPT ..." to send one question to a bigger brain.
ROUTES = [
    (re.compile(r"^\s*(?:hey\s+)?ask\s+claude[\s,:]+", re.I), "claude"),
    (re.compile(r"^\s*(?:hey\s+)?ask\s+(?:chat\s?gpt|codex|gpt)[\s,:]+", re.I), "codex"),
]


class Router:
    def __init__(self):
        self.default = os.getenv("BRAIN", "ollama").strip().lower()
        self._brains: dict = {}

    def brain(self, name: str):
        name = name if name in BRAINS else "ollama"
        if name not in self._brains:
            self._brains[name] = BRAINS[name]()
        return self._brains[name]

    def ask(self, text: str) -> str:
        for pattern, name in ROUTES:
            if pattern.match(text):
                return self.brain(name).ask(pattern.sub("", text, count=1))
        return self.brain(self.default).ask(text)
