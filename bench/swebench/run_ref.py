#!/usr/bin/env python3
"""Reference run of the SWE-bench Verified 50-task sample: Claude Code (`claude -p`) driven by the same model as
Nokker Lokkal, through an Anthropic-to-OpenAI translator that you provide (ANTHROPIC_BASE_URL). Same sample, same
task prompt (`run.PROMPT`), same turn cap (`--max-turns` = Lokkal's `--max-calls`), the same evaluation image as the
sandbox: the harness is measured, the model is held equal.

Per task: the image's /testbed is copied to the host (as in run.py), then Claude Code runs INSIDE the evaluation image
(its standalone binary mounted read-only, `--network host` so that a translator listening on 127.0.0.1 is reachable),
permissions skipped (disposable sandbox), a throwaway HOME. The repository diff becomes the prediction;
`run.py eval/report --run <run>` judge and report it.

  ANTHROPIC_BASE_URL=http://127.0.0.1:4000 CC_BIN=/path/to/claude python3 bench/swebench/run_ref.py solve --run ref100

ANTHROPIC_BASE_URL (required): an Anthropic Messages endpoint that forwards to the model under test, for example a
LiteLLM proxy in front of an OpenAI-compatible endpoint. ANTHROPIC_API_KEY: whatever that translator expects (a
placeholder is sent if unset). CC_BIN (required): the Claude Code standalone binary, run inside the image.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402

MODEL_NAME = "reference+qwen3.5-397b-a17b"
# Nom de modèle envoyé par le client ; c'est le traducteur qui choisit l'amont (passe publiée : ce nom, traduit vers
# Qwen3.5-397B-A17B).
DEFAULT_CLIENT_MODEL = "claude-sonnet-5-5"
DISALLOWED = ["WebSearch", "WebFetch", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Workflow",
              "RemoteTrigger"]
_lock = threading.Lock()


def settings() -> tuple[str, str, dict]:
    """(url du traducteur, binaire, environnement du processus docker) ; erreur claire s'il manque quelque chose."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base:
        sys.exit("run_ref.py: set ANTHROPIC_BASE_URL to an Anthropic-to-OpenAI translator in front of the model under "
                 "test (for example a LiteLLM proxy); this script does not start one.")
    cc_bin = os.environ.get("CC_BIN", "")
    if not cc_bin or not os.path.isfile(cc_bin):
        sys.exit("run_ref.py: set CC_BIN to the Claude Code standalone binary (it is mounted into the image).")
    # la clé passe par son nom (`-e ANTHROPIC_API_KEY`), jamais sur la ligne de commande
    env = {**os.environ, "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY") or "unused"}
    return base, os.path.realpath(cc_bin), env


def usage_of(final: dict) -> dict:
    """Jetons rapportés par le client dans l'événement `result` (cumul de la tâche). Ils valent ce que le traducteur
    renvoie : un traducteur qui ne transmet pas l'usage d'entrée donne 0 ici (voir README.md, « Input tokens »)."""
    u = final.get("usage") or {}
    return {"prompt_tokens": sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                 "cache_creation_input_tokens")),
            "completion_tokens": int(u.get("output_tokens") or 0)}


def solve_one(run: str, row: dict, max_turns: int, budget: dict, cfg: tuple[str, str, dict], client_model: str) -> dict:
    base, cc_bin, proc_env = cfg
    iid = row["instance_id"]
    out_dir = os.path.join(HERE, "results", run)
    os.makedirs(out_dir, exist_ok=True)
    res_path = os.path.join(out_dir, f"{iid}.json")
    if os.path.exists(res_path):
        return json.load(open(res_path))
    with _lock:
        if budget["spent"] >= budget["max"]:
            print(f"SAUTÉ {iid} : plafond de jetons atteint ({budget['spent']})", flush=True)
            return {"instance_id": iid, "skipped": "budget"}
    image = R.eval_image(iid)
    if R.sh(["docker", "pull", "-q", image]).returncode != 0:
        return {"instance_id": iid, "error": f"image introuvable : {image}"}
    ws = os.path.join(R.WORK, run, iid)
    home = os.path.join(R.WORK, run, f"{iid}.home")
    R.sh(["rm", "-rf", ws, home])
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    cid = R.sh(["docker", "create", image]).stdout.strip()
    R.sh(["docker", "cp", f"{cid}:/testbed", ws], check=True)
    R.sh(["docker", "rm", cid])
    if R.sh(["git", "-C", ws, "rev-parse", "HEAD"]).stdout.strip() != row["base_commit"]:
        R.sh(["git", "-C", ws, "checkout", "-q", row["base_commit"]], check=True)
    env = {"HOME": "/cchome", "CLAUDE_CONFIG_DIR": "/cchome/.claude", "IS_SANDBOX": "1", "LANG": "C.UTF-8",
           "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_COST_WARNINGS": "1",
           "ANTHROPIC_BASE_URL": base, "ANTHROPIC_MODEL": client_model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": client_model,
           "ANTHROPIC_SMALL_FAST_MODEL": client_model, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "262144"}
    name = f"ref-swe-{run}-{iid}".lower().replace("__", "-")[:120]
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "host", "-v", f"{ws}:/testbed",
           "-v", f"{home}:/cchome", "-v", f"{cc_bin}:/usr/local/bin/claude:ro", "-w", "/testbed",
           "-e", "ANTHROPIC_API_KEY"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, "claude", "-p", "--strict-mcp-config", "--dangerously-skip-permissions",
            "--disallowedTools", ",".join(DISALLOWED), "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--model", client_model, "--max-turns", str(max_turns),
            R.PROMPT.format(repo=row["repo"], problem=row["problem_statement"])]
    t0 = time.time()
    timed_out = False
    try:
        p = R.sh(cmd, timeout=R.TIMEOUT_S, env=proc_env)
        stdout, stderr = p.stdout, p.stderr
    except R.subprocess.TimeoutExpired as e:
        timed_out = True
        R.sh(["docker", "rm", "-f", name])
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
    wall = round(time.time() - t0, 1)
    open(os.path.join(out_dir, f"{iid}.stream.jsonl"), "w").write(stdout)
    open(os.path.join(out_dir, f"{iid}.stderr.txt"), "w").write(stderr[-200_000:])
    final: dict = {}
    n_tool_calls = 0
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = ev
        elif ev.get("type") == "assistant":
            n_tool_calls += sum(1 for b in (ev.get("message") or {}).get("content") or []
                                if isinstance(b, dict) and b.get("type") == "tool_use")
    R.sh(["git", "-C", ws, "add", "-A"])
    patch = R.sh(["git", "-C", ws, "diff", "--cached", "--binary", row["base_commit"]]).stdout
    tokens = usage_of(final)
    with _lock:
        budget["spent"] += tokens["prompt_tokens"]
    res = {"instance_id": iid, "repo": row["repo"], "difficulty": row.get("difficulty"), "timed_out": timed_out,
           "wall_s": wall, "error": final.get("subtype") if final.get("is_error") else None,
           "stop": final.get("subtype"), "num_turns": final.get("num_turns"), **tokens,
           "n_tool_calls": n_tool_calls, "patch_lines": patch.count("\n"), "reply": (final.get("result") or "")[-1500:]}
    json.dump(res, open(res_path, "w"), ensure_ascii=False, indent=1)
    with _lock, open(os.path.join(out_dir, "predictions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"instance_id": iid, "model_name_or_path": MODEL_NAME, "model_patch": patch}) + "\n")
    R.sh(["rm", "-rf", home])
    print(f"{iid:<45} {wall:6.0f}s  tours={final.get('num_turns') or 0:3}  in={tokens['prompt_tokens']:>9}  "
          f"patch={res['patch_lines']:4} l{'  TIMEOUT' if timed_out else ''}  cumul={budget['spent']}", flush=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve")
    s.add_argument("--run", required=True)
    s.add_argument("--parallel", type=int, default=3)
    s.add_argument("--max-turns", type=int, default=100)
    s.add_argument("--max-input-tokens", type=int, default=90_000_000)
    s.add_argument("--only", default="")
    s.add_argument("--client-model", default=DEFAULT_CLIENT_MODEL,
                   help="model name Claude Code sends; the translator maps it to the model under test")
    a = ap.parse_args()
    cfg = settings()
    ids = R.instances()
    if a.only:
        ids = [i for i in ids if i in set(a.only.split(","))]
    rows = R.dataset_rows(ids)
    budget = {"spent": 0, "max": a.max_input_tokens}
    done = os.path.join(HERE, "results", a.run)
    for f in os.listdir(done) if os.path.isdir(done) else []:
        if f.endswith(".json") and "__" in f and f".{a.run}." not in f:
            budget["spent"] += json.load(open(os.path.join(done, f))).get("prompt_tokens", 0)
    with ThreadPoolExecutor(a.parallel) as pool:
        list(pool.map(lambda i: solve_one(a.run, rows[i], a.max_turns, budget, cfg, a.client_model), ids))
    print(f"jetons d'entrée cumulés : {budget['spent']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
