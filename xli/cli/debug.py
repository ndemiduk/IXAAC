"""`/debug` slash command — fresh-context verifier subagent.

Spawns a WorkerAgent with no working-context memory, hands it the original
user task + a `git diff HEAD` of changes made since the last commit, and asks
it to find specific bugs and scope violations. Output is printed to console
and NOT appended to history (verifier output is meta — it shouldn't bias the
next turn unless the user reacts to it).
"""

from __future__ import annotations

import subprocess
from typing import Optional

from rich.console import Console

console = Console()


VERIFIER_SYSTEM_PROMPT = """You are a code verifier. You did NOT write the code under review — you are examining it cold, with no memory of the decisions that produced it.

You will be given:
1. The original user task.
2. A git diff of all changes made in response to that task.

Your job: find specific bugs and scope violations. Not style. Not "could be cleaner." Real defects.

Anchored checklist:
- Does the diff parse / import cleanly? (Use bash to verify if uncertain — e.g. `python -c "import xli.foo"`.)
- Does the scope of changes match the user's request, or did unrelated files get touched?
- Are there verification claims in commit messages or comments that aren't actually verified by the diff?
- Are there obvious correctness bugs: off-by-one, wrong types, swapped args, missing branches, broken error paths, dead code paths?
- Are there security regressions: unredacted secrets, removed validation, widened permissions, new subprocess calls without escaping?

Output format:
- If clean: one line — "PASS: <one-sentence summary of what changed>".
- If issues: "FAIL" then a numbered list. Each item: file:line, one sentence describing the specific defect. No suggestions for new features. No praise. No restating the diff.

Do not propose fixes unless asked. Do not modify any files. You have read-only investigation tools."""


def _last_user_prompt(agent) -> Optional[str]:
    """Return the most recent user prompt sent to run_turn, or None.

    Prefers agent.last_user_message (captured at run_turn entry, survives
    history condensation). Falls back to walking agent.history for the
    pre-run_turn case (e.g. /debug right after session start)."""
    captured = getattr(agent, "last_user_message", None)
    if isinstance(captured, str) and captured.strip():
        return captured
    for msg in reversed(getattr(agent, "history", []) or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


def _git_diff(cwd, ref: str) -> tuple[str, Optional[str]]:
    """Return (diff_text, error). diff_text is the diff from `ref` to the
    current working tree (covers both committed and uncommitted changes)."""
    try:
        proc = subprocess.run(
            ["git", "diff", ref],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return ("", "git not installed")
    except subprocess.TimeoutExpired:
        return ("", f"git diff {ref} timed out after 10s")
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"git diff {ref} failed"
        return ("", err)
    return (proc.stdout, None)


def _changed_files(cwd, ref: str) -> list[str]:
    """Return list of filenames touched between `ref` and current working tree."""
    try:
        proc = subprocess.run(
            ["git", "diff", ref, "--name-only"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _handle_debug_command(user_input: str, agent, project) -> bool:
    """Handle `/debug` — spawn a fresh-context verifier subagent on the last turn.

    Reviews `git diff HEAD` against the most recent user prompt. Verifier
    output prints to console only; nothing goes back into history.
    """
    if user_input != "/debug" and not user_input.startswith("/debug "):
        return False

    last_prompt = _last_user_prompt(agent)
    if last_prompt is None:
        console.print("[dim]/debug: no prior chat turn this session — nothing to verify[/dim]")
        return True

    # Prefer the per-turn baseline SHA so /debug captures everything since the
    # turn started, including any commits made during the turn. Fall back to
    # HEAD if no baseline (e.g. plain `xli ask` outside a turn loop).
    ref = getattr(agent, "last_turn_baseline_sha", None) or "HEAD"
    diff_text, err = _git_diff(project.project_root, ref)
    if err is not None:
        console.print(f"[red]/debug: {err}[/red]")
        return True
    if not diff_text.strip():
        scope = (
            f"since turn started (baseline {ref[:8]})"
            if ref != "HEAD" else "vs HEAD"
        )
        console.print(
            f"[dim]/debug: no changes {scope} — nothing to verify.[/dim]"
        )
        return True

    files = _changed_files(project.project_root, ref)
    files_str = "\n".join(f"  - {f}" for f in files) if files else "  (none enumerated)"
    scope = (
        f"since turn baseline ({ref[:12]} → working tree, includes any commits made during the turn)"
        if ref != "HEAD" else
        "uncommitted vs HEAD"
    )

    brief = (
        f"Original task:\n{last_prompt}\n\n"
        f"Files changed ({scope}):\n{files_str}\n\n"
        f"Diff:\n{diff_text}"
    )

    # Spawn the verifier — same recipe as Agent._dispatch_subagent_call but
    # with the verifier system prompt override.
    from xli.agent import WorkerAgent
    from xli.plugin import load_subscriptions

    worker_clients = agent.pool.acquire()
    worker = WorkerAgent(
        clients=worker_clients,
        project=project,
        cfg=agent.cfg,
        extra_collection_ids=[cid for _, cid in agent.attached_refs],
        subscribed_plugins=load_subscriptions(project.xli_dir),
    )

    n_files = len(files)
    diff_lines = diff_text.count("\n")
    console.print(
        f"[dim]/debug: spawning verifier on {n_files} file(s), "
        f"{diff_lines} diff line(s)…[/dim]"
    )

    try:
        text, call = worker.run(
            task=brief,
            system_prompt_override=VERIFIER_SYSTEM_PROMPT,
        )
    except Exception as e:
        console.print(f"[red]/debug: verifier crashed: {type(e).__name__}: {e}[/red]")
        return True

    console.print()
    console.rule("[bold]verifier[/bold]", style="cyan")
    console.print(text)
    console.rule(style="cyan")

    from xli.cost import format_cost, format_tokens
    cost_part = (
        f" · {format_cost(call.cost_usd)}" if call.cost_usd is not None else ""
    )
    console.print(
        f"[dim]verifier[{worker_clients.label}] · {call.model} · "
        f"{call.iterations} iter · {format_tokens(call.total_tokens)}{cost_part}[/dim]"
    )

    return True
