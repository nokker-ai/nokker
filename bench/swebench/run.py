#!/usr/bin/env python3
"""SWE-bench Verified, 50-task sample (instances.txt), solved by Nokker Lokkal (`nokker code`), judged by the official
SWE-bench harness.

Per task:
  1. the official evaluation image (`swebench/sweb.eval.x86_64.<id>`): the repository in /testbed at the base commit,
     the `testbed` conda environment installed; the image's /testbed is copied to the host;
  2. `nokker code` runs with THAT image as its sandbox and the copied repository mounted on /testbed (`--mount`),
     where the editable install points: the agent's tests run on its own code, in the exact environment of the task;
  3. the repository diff (new files included) becomes the prediction.
Then `eval` runs `swebench.harness.run_evaluation` on the predictions (official images, hidden tests), and `report`
prints the score and the input tokens.

The model is any OpenAI-compatible endpoint: OPENAI_API_KEY (required), OPENAI_BASE_URL and OPENAI_MODEL (default:
the endpoint and model of the published runs), read from the environment or from `--env-file FILE` (KEY=VALUE lines).

  python3 bench/swebench/run.py solve --run v3 [--env-file FILE] [--parallel 3] [--only <id>,...]
  python3 bench/swebench/run.py eval --run v3 [--workers 8]
  python3 bench/swebench/run.py report --run v3

Spending guard: `--max-input-tokens` (input tokens summed over all tasks, 90 M by default); past it, the remaining
tasks are not started. Resuming: a task that already has a result is skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WORK = os.environ.get("SWEBENCH_WORK", "/var/tmp/swebench")
IMAGE = os.environ.get("LOKKAL_IMAGE", "ghcr.io/nokker-ai/nokker:0.1.0-rc.5-lokkal")  # bibliothèque amont + dépendances du noyau
# Point d'accès et modèle des passes publiées (OVH AI Endpoints, URL publique) ; OPENAI_BASE_URL / OPENAI_MODEL les
# remplacent. La clé n'a pas de défaut : OPENAI_API_KEY, dans l'environnement ou dans --env-file.
DEFAULT_BASE_URL = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
DEFAULT_MODEL = "Qwen3.5-397B-A17B"
ENDPOINT_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
DATASET = "princeton-nlp/SWE-bench_Verified"
MODEL_NAME = "nokker-lokkal+qwen3.5-397b-a17b"
TIMEOUT_S = 2400
PROMPT = """You are working in the git repository of {repo}, checked out at /testbed. Resolve the issue below: change \
the code so that the behaviour asked for in the issue holds.

<issue>
{problem}
</issue>

The project's Python environment is installed: run `source /opt/miniconda3/bin/activate testbed` in a command before \
running Python or the tests. Keep the change minimal and correct. Do not edit existing tests unless the fix requires \
it; you may add new tests. You are working alone, nobody will answer questions: do not ask any, go to the end, and run \
the relevant tests to check your work before you finish."""

_spent_lock = threading.Lock()


def instances() -> list[str]:
    with open(os.path.join(HERE, "instances.txt"), encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def dataset_rows(ids: list[str]) -> dict[str, dict]:
    # sur la machine du banc : pip install "swebench==4.1.0" datasets — la 5.x lit l'image dans un champ `image` que
    # princeton-nlp/SWE-bench_Verified n'a pas, et n'a plus --namespace
    from datasets import load_dataset

    wanted = set(ids)
    return {r["instance_id"]: r for r in load_dataset(DATASET, split="test") if r["instance_id"] in wanted}


def eval_image(iid: str) -> str:
    return f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest"


def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE (option `export`, guillemets tolérés), lignes vides et # ignorées."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.removeprefix("export ").split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def endpoint(env_file: str | None) -> dict[str, str]:
    """OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL : l'environnement passe avant --env-file."""
    from_file = read_env_file(env_file) if env_file else {}
    ep = {k: os.environ.get(k) or from_file.get(k, "") for k in ENDPOINT_VARS}
    if not ep["OPENAI_API_KEY"]:
        sys.exit("run.py solve: no model key. Set OPENAI_API_KEY (and optionally OPENAI_BASE_URL, OPENAI_MODEL) in "
                 "the environment, or pass --env-file FILE with these KEY=VALUE lines.")
    ep["OPENAI_BASE_URL"] = ep["OPENAI_BASE_URL"] or DEFAULT_BASE_URL
    ep["OPENAI_MODEL"] = ep["OPENAI_MODEL"] or DEFAULT_MODEL
    return ep


def lokkal_config(base_url: str, model: str) -> dict:
    """Config du noyau pour le banc : sans base (mémoire du fil en processus), sans mémoire longue, sans juge, sans
    approbations par appareil (une action retenue reste en attente et la tâche continue sans elle), sans recherche
    web. Les deux paliers sur le même modèle, sans cache de préfixe. Aucun secret : la clé est lue dans
    OPENAI_API_KEY par le noyau."""
    tier = {"base_url": base_url, "model": model, "api_key_env": "OPENAI_API_KEY", "cache": "none"}
    return {"memory_mcp_url": "", "prefix_path": "", "api_keys": [], "telegram_ids": {}, "extra_go_senders": [],
            "tiers": {"ship": dict(tier), "deep": dict(tier)}, "mcp_tool_servers": [], "judge_enabled": False,
            "approvals_enabled": False, "approvals_telegram_enabled": False, "web_search_enabled": False,
            "context_enabled": True, "context_corthexis": {"enabled": False}, "tenant_limits": {}}


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def solve_one(run: str, row: dict, max_calls: int, budget: dict, key_env: dict) -> dict:
    iid = row["instance_id"]
    out_dir = os.path.join(HERE, "results", run)
    os.makedirs(out_dir, exist_ok=True)
    res_path = os.path.join(out_dir, f"{iid}.json")
    if os.path.exists(res_path):
        return json.load(open(res_path))
    with _spent_lock:
        if budget["spent"] >= budget["max"]:
            print(f"SAUTÉ {iid} : plafond de jetons atteint ({budget['spent']})", flush=True)
            return {"instance_id": iid, "skipped": "budget"}
    image = eval_image(iid)
    if sh(["docker", "pull", "-q", image]).returncode != 0:
        return {"instance_id": iid, "error": f"image introuvable : {image}"}
    ws = os.path.join(WORK, run, iid)
    sh(["rm", "-rf", ws])
    os.makedirs(os.path.dirname(ws), exist_ok=True)
    cid = sh(["docker", "create", image]).stdout.strip()
    sh(["docker", "cp", f"{cid}:/testbed", ws], check=True)
    sh(["docker", "rm", cid])
    head = sh(["git", "-C", ws, "rev-parse", "HEAD"]).stdout.strip()
    if head != row["base_commit"]:
        sh(["git", "-C", ws, "checkout", "-q", row["base_commit"]], check=True)
    name = f"lokkal-swe-{run}-{iid}".lower().replace("__", "-")[:120]
    cmd = ["docker", "run", "--rm", "--name", name,
           # -w /w : avec `python -m`, le dossier courant passe avant PYTHONPATH (sinon le noyau de l'image tourne)
           "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{ws}:{ws}", "-v", f"{REPO}:/w:ro", "-w", "/w",
           "-e", "PYTHONPATH=/w", "-e", "HOME=/tmp", "-e", "HERMES_HOME=/tmp/hh",
           # la clé passe par son nom (valeur dans l'environnement du processus docker, jamais sur la ligne de commande)
           "-e", "OPENAI_API_KEY",
           "--entrypoint", "python", IMAGE, "-m", "nokker",
           "--config", f"/w/bench/swebench/results/{run}/lokkal-config.json",
           "code", PROMPT.format(repo=row["repo"], problem=row["problem_statement"]), "--workspace", ws,
           "--image", image, "--mount", "/testbed", "--max-calls", str(max_calls), "--mode", "auto",
           # bac à sable jetable, aucun humain : toute commande permise (les « hardline » restent refusées par la
           # source), comme la référence qui tourne sans contrôle de permissions. Sans ça (passe v50 du 24.10) :
           # 181 commandes retenues sur 43 tâches, et 4 correctifs vides.
           "--allow", "terminal(*)", "--json"]
    t0 = time.time()
    timed_out = False
    try:
        p = sh(cmd, timeout=TIMEOUT_S, env=key_env)
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        sh(["docker", "rm", "-f", name])
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
    wall = round(time.time() - t0, 1)
    open(os.path.join(out_dir, f"{iid}.stderr.txt"), "w").write(stderr[-200_000:])
    try:
        report = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        report = {"unparsed_stdout": stdout[-4000:]}
    sh(["git", "-C", ws, "add", "-A"])
    patch = sh(["git", "-C", ws, "diff", "--cached", "--binary", row["base_commit"]]).stdout
    calls = report.get("llm_calls", [])
    prompt_tokens = sum(c.get("prompt", 0) for c in calls)
    with _spent_lock:
        budget["spent"] += prompt_tokens
    res = {"instance_id": iid, "repo": row["repo"], "difficulty": row.get("difficulty"), "timed_out": timed_out,
           "wall_s": wall, "error": report.get("error"), "n_llm_calls": len(calls),
           "n_tool_calls": len(report.get("tool_calls", [])), "prompt_tokens": prompt_tokens,
           "completion_tokens": sum(c.get("completion", 0) for c in calls), "patch_lines": patch.count("\n"),
           "reasoning_tokens": sum(c.get("reasoning", 0) for c in calls),
           "pending_approvals": report.get("pending_approvals", []), "pending_calls": report.get("pending_calls", []),
           "loop_rules": report.get("loop_rules", []), "reply": (report.get("reply") or "")[-1500:]}
    json.dump(report.get("tool_calls_full", []), open(os.path.join(out_dir, f"{iid}.tools.json"), "w"),
              ensure_ascii=False)
    json.dump(res, open(res_path, "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "predictions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"instance_id": iid, "model_name_or_path": MODEL_NAME, "model_patch": patch}) + "\n")
    print(f"{iid:<45} {wall:6.0f}s  modèle={len(calls):3}  in={prompt_tokens:>9}  patch={res['patch_lines']:4} l"
          f"{'  TIMEOUT' if timed_out else ''}  cumul={budget['spent']}", flush=True)
    return res


def cmd_solve(a: argparse.Namespace) -> int:
    ids = instances()
    if a.only:
        ids = [i for i in ids if i in set(a.only.split(","))]
    ep = endpoint(a.env_file)
    rows = dataset_rows(ids)
    budget = {"spent": 0, "max": a.max_input_tokens}
    done = os.path.join(HERE, "results", a.run)
    os.makedirs(done, exist_ok=True)
    # JSON = YAML valide : le noyau lit ce fichier tel quel (le dépôt est monté sur /w)
    with open(os.path.join(done, "lokkal-config.json"), "w", encoding="utf-8") as f:
        json.dump(lokkal_config(ep["OPENAI_BASE_URL"], ep["OPENAI_MODEL"]), f, indent=1)
    key_env = {**os.environ, "OPENAI_API_KEY": ep["OPENAI_API_KEY"]}
    for f in os.listdir(done) if os.path.isdir(done) else []:
        if f.endswith(".json") and "__" in f and not f.endswith(".tools.json") and f".{a.run}." not in f:
            budget["spent"] += json.load(open(os.path.join(done, f))).get("prompt_tokens", 0)
    with ThreadPoolExecutor(a.parallel) as pool:
        list(pool.map(lambda i: solve_one(a.run, rows[i], a.max_calls, budget, key_env), ids))
    print(f"jetons d'entrée cumulés : {budget['spent']}")
    return 0


def cmd_eval(a: argparse.Namespace) -> int:
    preds = os.path.join(HERE, "results", a.run, "predictions.jsonl")
    # le harnais 4.1.0 écrit son rapport dans le dossier courant (--report_dir ne vise que les journaux)
    return subprocess.call([sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", DATASET,
                            "--predictions_path", preds, "--max_workers", str(a.workers), "--run_id", a.run,
                            "--namespace", "swebench", "--report_dir", os.path.join(HERE, "results", a.run)],
                           cwd=os.path.join(HERE, "results", a.run))


def cmd_report(a: argparse.Namespace) -> int:
    out = os.path.join(HERE, "results", a.run)
    rep = next((os.path.join(out, f) for f in os.listdir(out) if f.endswith(f".{a.run}.json")), None)
    if rep is None:
        print("rapport du harnais introuvable (lancer eval)", file=sys.stderr)
        return 1
    r = json.load(open(rep))
    resolved = set(r.get("resolved_ids", []))
    per = [json.load(open(os.path.join(out, f))) for f in sorted(os.listdir(out))
           if f.endswith(".json") and "__" in f and not f.endswith((f".{a.run}.json", ".tools.json"))]
    total_in = sum(x.get("prompt_tokens", 0) for x in per)
    print(f"résolues : {len(resolved)}/{len(instances())}  (soumises {r.get('submitted_instances')}, "
          f"erreurs {r.get('error_instances')}, vides {r.get('empty_patch_instances')})")
    print(f"jetons d'entrée : {total_in}  temps médian : "
          f"{sorted(x.get('wall_s', 0) for x in per)[len(per) // 2] if per else 0:.0f} s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve")
    s.add_argument("--run", required=True)
    s.add_argument("--parallel", type=int, default=3)
    s.add_argument("--max-calls", type=int, default=100)  # même plafond que la référence (--max-turns)
    s.add_argument("--max-input-tokens", type=int, default=90_000_000)
    s.add_argument("--only", default="")
    s.add_argument("--env-file", default=None,
                   help="KEY=VALUE file with OPENAI_API_KEY [, OPENAI_BASE_URL, OPENAI_MODEL]")
    s.set_defaults(func=cmd_solve)
    e = sub.add_parser("eval")
    e.add_argument("--run", required=True)
    e.add_argument("--workers", type=int, default=8)
    e.set_defaults(func=cmd_eval)
    r = sub.add_parser("report")
    r.add_argument("--run", required=True)
    r.set_defaults(func=cmd_report)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
