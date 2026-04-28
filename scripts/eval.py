#!/usr/bin/env python3
"""
eval.py — Agentic model evaluation against OpenSearch traffic logs.

Runs identical tasks against multiple models through the ollama proxy,
then queries OpenSearch to compare performance metrics.

Dependencies:
    pip install fire httpx opensearch-py rich pyyaml

Commands:
    python eval.py run      --tasks tasks.yaml --models qwen3:27b,llama3
    python eval.py compare  --run-id 20260425-120000
    python eval.py runs
    python eval.py validate --tasks tasks.yaml
"""

import json
import re
import subprocess
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fire
import httpx
import yaml
from opensearchpy import OpenSearch
from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

PROXY_URL      = "http://localhost:11434"
OPENSEARCH_URL = "http://localhost:9200"
INDEX          = "ollama-traffic"


# ── helpers ───────────────────────────────────────────────────────────────────

def _os_client(url: str) -> OpenSearch:
    scheme = "https" if url.startswith("https") else "http"
    host   = url.replace("https://", "").replace("http://", "")
    h, *rest = host.split(":")
    port = int(rest[0]) if rest else 9200
    return OpenSearch(
        [{"host": h, "port": port}],
        use_ssl=scheme == "https",
        verify_certs=False,
    )


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:24]


def _load_tasks(path: str) -> list[dict]:
    p = Path(path)
    text = p.read_text()
    data = yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)
    tasks = data.get("tasks", data)
    assert isinstance(tasks, list), "tasks file must have a 'tasks' list"
    for t in tasks:
        assert "name" in t,     f"task missing 'name': {t}"
        assert "messages" in t, f"task '{t.get('name')}' missing 'messages'"
    return tasks


def _drain_stream(response: httpx.Response) -> None:
    for _ in response.iter_bytes():
        pass


# ── metric helpers ────────────────────────────────────────────────────────────

_ERROR_KEYWORDS = frozenset([
    "error", "not found", "failed", "exception",
    "traceback", "command not found", "no such file",
])

_PREAMBLE_RE = re.compile(
    r"^(sure[,!]?|of course|let me|i'll|i will|here is|here's|"
    r"based on|the answer is|great[,!]?|certainly)\b",
    re.IGNORECASE,
)


def _count_tool_calls(messages: list) -> int:
    return sum(
        1 for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    )


def _count_tool_errors(messages: list) -> int:
    return sum(
        1 for m in messages
        if m.get("role") == "tool"
        and any(kw in m.get("content", "").lower() for kw in _ERROR_KEYWORDS)
    )


def _count_retries(messages: list) -> int:
    retries, prev = 0, None
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                name = tc.get("function", {}).get("name")
                if name and name == prev:
                    retries += 1
                prev = name
    return retries


def _response_lines(content: Optional[str]) -> int:
    if not content:
        return 0
    return len([l for l in content.strip().splitlines() if l.strip()])


def _has_preamble(content: Optional[str]) -> bool:
    if not content:
        return False
    first = content.strip().splitlines()[0]
    return bool(_PREAMBLE_RE.match(first))


def _avg(lst: list) -> float:
    return round(statistics.mean(lst), 2) if lst else 0.0


def _p95(lst: list) -> float:
    if not lst:
        return 0.0
    return round(sorted(lst)[max(0, int(len(lst) * 0.95) - 1)], 2)


def _compute_metrics(docs: list[dict]) -> dict:
    if not docs:
        return {}

    total_tok   = [d["total_tokens"]      for d in docs if d.get("total_tokens")]
    prompt_tok  = [d["prompt_tokens"]     for d in docs if d.get("prompt_tokens")]
    comp_tok    = [d["completion_tokens"] for d in docs if d.get("completion_tokens")]
    durations   = [d["duration_ms"]       for d in docs if d.get("duration_ms")]
    turns       = [d.get("turn_number", 1) for d in docs]

    tool_calls, tool_errors, retries, lines, preambles = [], [], [], [], []
    for d in docs:
        msgs = (d.get("request_body") or {}).get("messages", [])
        tool_calls.append(_count_tool_calls(msgs))
        tool_errors.append(_count_tool_errors(msgs))
        retries.append(_count_retries(msgs))
        lines.append(_response_lines(d.get("response_content")))
        preambles.append(_has_preamble(d.get("response_content")))

    total_tc  = sum(tool_calls)
    total_err = sum(tool_errors)
    ap = _avg(prompt_tok)
    ac = _avg(comp_tok)

    return {
        "n":                     len(docs),
        "avg_total_tokens":      _avg(total_tok),
        "avg_prompt_tokens":     ap,
        "avg_completion_tokens": ac,
        "completion_ratio":      round(ac / ap, 3) if ap else 0,
        "avg_duration_ms":       _avg(durations),
        "p95_duration_ms":       _p95(durations),
        "tokens_per_sec":        round(_avg(total_tok) / (_avg(durations) / 1000), 1) if durations else 0,
        "avg_turns":             _avg(turns),
        "avg_tool_calls":        _avg(tool_calls),
        "tool_error_rate":       round(total_err / total_tc, 3) if total_tc else 0,
        "avg_retries":           _avg(retries),
        "avg_response_lines":    _avg(lines),
        "preamble_rate":         round(sum(preambles) / len(preambles), 3) if preambles else 0,
    }


# ── display ───────────────────────────────────────────────────────────────────

# (key, display label, lower_is_better)
METRIC_DEFS = [
    ("n",                     "Samples",            None),
    ("avg_total_tokens",      "Avg tokens",         True),
    ("avg_prompt_tokens",     "Avg prompt tokens",  True),
    ("avg_completion_tokens", "Avg completion tok", None),
    ("completion_ratio",      "Completion ratio",   None),
    ("tokens_per_sec",        "Tokens / sec",       False),
    ("avg_duration_ms",       "Avg duration (ms)",  True),
    ("p95_duration_ms",       "p95 duration (ms)",  True),
    ("avg_turns",             "Avg turns",          True),
    ("avg_tool_calls",        "Avg tool calls",     True),
    ("tool_error_rate",       "Tool error rate",    True),
    ("avg_retries",           "Avg retries",        True),
    ("avg_response_lines",    "Avg resp lines",     None),
    ("preamble_rate",         "Preamble rate",      True),
]


def _winner_indices(values: list, lower_is_better: Optional[bool]) -> set[int]:
    if lower_is_better is None or not any(v > 0 for v in values):
        return set()
    best = min(values) if lower_is_better else max(values)
    return {i for i, v in enumerate(values) if v == best}


def _render_table(metrics_by_model: dict, title: str) -> None:
    models = list(metrics_by_model.keys())

    table = Table(title=title, box=box.ROUNDED, show_lines=True, title_style="bold")
    table.add_column("Metric", style="bold", no_wrap=True)
    for m in models:
        table.add_column(m, justify="right")

    for key, label, lower_is_better in METRIC_DEFS:
        values  = [metrics_by_model[m].get(key, 0) for m in models]
        winners = _winner_indices(values, lower_is_better)
        row = [label]
        for i, v in enumerate(values):
            cell = str(v)
            if i in winners:
                cell = f"[green]{v}[/green]"
            row.append(cell)
        table.add_row(*row)

    console.print(table)
    console.print(
        "[dim]Green = best value per metric. "
        "Completion ratio = completion_tokens / prompt_tokens. "
        "Tool error rate = errors / total tool calls.[/dim]\n"
    )


# ── CLI commands ──────────────────────────────────────────────────────────────

class Eval:
    """Agentic model evaluation tool."""

    def __init__(
        self,
        proxy: str = PROXY_URL,
        opensearch: str = OPENSEARCH_URL,
        index: str = INDEX,
    ):
        self._proxy = proxy.rstrip("/")
        self._os    = _os_client(opensearch)
        self._index = index

    def run(
        self,
        tasks: str,
        models: str,
        runs: int = 3,
        delay: float = 1.5,
        run_id: Optional[str] = None,
        max_tokens: int = 4096,
        agent: Optional[str] = None,
        agent_cmd: Optional[str] = None,
    ):
        """
        Send tasks to each model and record the run.

        Args:
            tasks:      Path to tasks YAML/JSON file.
            models:     Comma-separated model names, e.g. "qwen3:27b,llama3".
            runs:       How many times to repeat each task per model.
            delay:      Seconds to wait between requests (avoid rate-limiting).
            run_id:     Override the auto-generated run ID.
            max_tokens: Default max_tokens for all tasks (overridden per task).
            agent:      External agent backend: "opencode" or "claude".
            agent_cmd:  Override agent command template. Use {prompt} and {model} as
                        placeholders. Default per agent type.
        """
        run_id     = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        model_list = [m.strip() for m in models.split(",")]
        task_list  = _load_tasks(tasks)
        use_agent  = agent is not None

        console.print(f"\n[bold]Run:[/bold]    {run_id}")
        console.print(f"[bold]Models:[/bold] {', '.join(model_list)}")
        console.print(f"[bold]Agent:[/bold]  {agent}" if use_agent else "")
        console.print(f"[bold]Tasks:[/bold]  {len(task_list)} × {runs} runs × {len(model_list)} models "
                      f"= {len(task_list) * runs * len(model_list)} requests\n")

        # Preload models (skip when using external agent — agent handles loading)
        if not use_agent:
            for model in model_list:
                console.print(f"  [bold]Loading[/bold] model {model}...")
                try:
                    with httpx.Client(timeout=600.0) as client:
                        resp = client.post(
                            f"{self._proxy}/api/generate",
                            json={"model": model, "prompt": ".", "stream": False},
                        )
                        _drain_stream(resp)
                        color = "green" if resp.status_code == 200 else "yellow"
                        console.print(f"  [{color}]✓[/{color}] {model} loaded ({resp.status_code})")
                except Exception as e:
                    console.print(f"  [yellow]⚠[/yellow] Failed to preload {model}: {e}")


        sent: list[str] = []

        for model in model_list:
            for task in task_list:
                for r in range(runs):
                    session_id = f"eval_{run_id}_{_slug(model)}_{_slug(task['name'])}_{r}"
                    label = f"{model:30s} · {task['name']:20s} · run {r+1}/{runs}"

                    if use_agent:
                        ok = self._run_via_agent(
                            model, task, label, run_id, session_id, max_tokens,
                            agent, agent_cmd,
                        )
                    else:
                        ok = self._run_via_ollama(
                            model, task, label, run_id, session_id, max_tokens,
                        )

                    if ok:
                        sent.append(session_id)

                    time.sleep(delay)

        manifest = {
            "run_id":      run_id,
            "models":      model_list,
            "tasks":       [t["name"] for t in task_list],
            "runs":        runs,
            "session_ids": sent,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        out = Path(f"eval_run_{run_id}.json")
        out.write_text(json.dumps(manifest, indent=2))
        console.print(f"\n[bold green]Done.[/bold green] Manifest → {out}")
        console.print(f"Compare: [bold]python eval.py compare --run-id {run_id}[/bold]\n")

    def compare(
        self,
        run_id: str,
        task: Optional[str] = None,
        format: str = "table",
    ):
        """
        Compare metrics across models for a completed run.

        Args:
            run_id: Run ID from a previous `run` command.
            task:   Filter to a single task name (default: all tasks).
            format: Output format: 'table' or 'json'.
        """
        manifest_path = Path(f"eval_run_{run_id}.json")
        if not manifest_path.exists():
            console.print(f"[red]Manifest not found:[/red] {manifest_path}")
            raise SystemExit(1)

        manifest   = json.loads(manifest_path.read_text())
        model_list = manifest["models"]
        task_names = [task] if task else manifest["tasks"]

        metrics_by_model: dict[str, dict] = {}

        for model in model_list:
            model_slug = _slug(model)
            ids = [
                sid for sid in manifest["session_ids"]
                if f"_{model_slug}_" in sid
                and any(f"_{_slug(tn)}_" in sid for tn in task_names)
            ]
            if not ids:
                console.print(f"[yellow]No sessions found for model {model}[/yellow]")
                continue

            # fetch from OpenSearch — exclude large message arrays from source
            resp = self._os.search(
                index=self._index,
                body={
                    "size": 1000,
                    "query": {"ids": {"values": ids}},
                    "_source": {
                        "excludes": [
                            "request_body.messages",
                            "request_body.tools",
                            "request_body.stream_options",
                        ]
                    },
                },
            )
            docs = [h["_source"] for h in resp["hits"]["hits"]]
            metrics_by_model[model] = _compute_metrics(docs)

            console.print(
                f"  [dim]{model}[/dim]: fetched {len(docs)} documents from OpenSearch"
            )

        if not metrics_by_model:
            console.print("[yellow]No data found for this run.[/yellow]")
            return

        if format == "json":
            console.print_json(json.dumps(metrics_by_model, indent=2))
            return

        task_label = f" · task={task}" if task else ""
        _render_table(metrics_by_model, f"Model Comparison — {run_id}{task_label}")

        # per-task breakdown if multiple tasks and no filter
        if not task and len(task_names) > 1:
            console.print("[bold]Per-task breakdown[/bold]\n")
            for tn in task_names:
                task_metrics: dict[str, dict] = {}
                for model in model_list:
                    model_slug = _slug(model)
                    ids = [
                        sid for sid in manifest["session_ids"]
                        if f"_{model_slug}_" in sid and f"_{_slug(tn)}_" in sid
                    ]
                    if not ids:
                        continue
                    resp = self._os.search(
                        index=self._index,
                        body={
                            "size": 200,
                            "query": {"ids": {"values": ids}},
                            "_source": {"excludes": ["request_body.messages", "request_body.tools"]},
                        },
                    )
                    task_metrics[model] = _compute_metrics(
                        [h["_source"] for h in resp["hits"]["hits"]]
                    )
                if task_metrics:
                    _render_table(task_metrics, f"Task: {tn}")

    def runs(self):
        """List past evaluation runs found in the current directory."""
        manifests = sorted(Path(".").glob("eval_run_*.json"), reverse=True)
        if not manifests:
            console.print("[yellow]No evaluation runs found in current directory.[/yellow]")
            return

        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column("Run ID",    style="bold")
        table.add_column("Models")
        table.add_column("Tasks")
        table.add_column("Runs")
        table.add_column("Timestamp")

        for p in manifests:
            m = json.loads(p.read_text())
            table.add_row(
                m["run_id"],
                ", ".join(m["models"]),
                ", ".join(m["tasks"]),
                str(m.get("runs", "?")),
                m["timestamp"][:19],
            )
        console.print(table)

    def validate(self, tasks: str):
        """Validate a tasks YAML/JSON file."""
        try:
            task_list = _load_tasks(tasks)
        except Exception as e:
            console.print(f"[red]✗ Failed to load:[/red] {e}")
            raise SystemExit(1)

        console.print(f"[green]✓[/green] {len(task_list)} tasks:\n")
        for t in task_list:
            msgs  = t.get("messages", [])
            roles = [m["role"] for m in msgs]
            tools = t.get("tools", [])
            console.print(f"  [bold]{t['name']}[/bold]")
            console.print(f"    messages : {len(msgs)}  ({' → '.join(roles)})")
            console.print(f"    tools    : {len(tools)}")
            if t.get("options"):
                console.print(f"    options  : {t['options']}")
            console.print()


if __name__ == "__main__":
    fire.Fire(Eval)
