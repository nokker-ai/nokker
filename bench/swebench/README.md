# SWE-bench Verified, 50-task sample

This folder reproduces the SWE-bench Verified numbers published in
[docs/en/benchmarks.md](../../docs/en/benchmarks.md): Nokker Lokkal, and Claude Code driven by the same model as a
reference, on the same 50 tasks, judged by the official SWE-bench harness.

| File | What it is |
|---|---|
| [instances.txt](instances.txt) | The 50 task ids |
| [run.py](run.py) | Solves the tasks with Nokker Lokkal (`solve`), runs the official judge (`eval`), prints the score (`report`) |
| [run_ref.py](run_ref.py) | Solves the same tasks with Claude Code driven by the same model (`solve`); judged by `run.py eval` |
| [reports/](reports/) | The official judge's report of each run, unedited |

## Sample

50 instances drawn once from `princeton-nlp/SWE-bench_Verified` (test split, 500 tasks) with
`random.Random(20261025).sample(sorted(ids), 50)` (seed 20261025), never edited by hand. The list is
[instances.txt](instances.txt): 1 astropy, 24 django, 2 requests, 3 xarray, 2 pylint, 1 pytest, 3 scikit-learn,
5 sphinx, 9 sympy.

## Judge

The official harness, `swebench` 4.1.0 (`python -m swebench.harness.run_evaluation`), with the official evaluation
images (`swebench/sweb.eval.x86_64.<id>`) and their hidden tests. A task is resolved only if its FAIL_TO_PASS tests
pass and no PASS_TO_PASS test breaks. The files in [reports/](reports/) are the judge's output as written, one per
run (`resolved_ids`, `unresolved_ids`, `empty_patch_ids`, `error_ids`).

`swebench` 4.1.0 is pinned on purpose: 5.x reads the image name from an `image` field that
`princeton-nlp/SWE-bench_Verified` does not have, and no longer takes `--namespace`.

## Conditions

The same for both harnesses:

- one attempt per task, no retries, the same task prompt (`PROMPT` in [run.py](run.py));
- at most 100 model calls per task (`--max-calls` for Lokkal, `--max-turns` for the reference), a 40-minute timeout;
- the task's official evaluation image as the sandbox: the repository is copied out of the image, mounted back on
  `/testbed`, and the agent's commands run in the image with its conda environment;
- every command allowed inside that disposable sandbox (`--allow 'terminal(*)'` for Lokkal,
  `--dangerously-skip-permissions` for the reference), no web search, no web fetch;
- model: Qwen3.5-397B-A17B on OVH AI Endpoints (`https://oai.endpoints.kepler.ai.cloud.ovh.net/v1`), provider
  defaults (thinking enabled), 16 384 output tokens per call, no prompt cache;
- hardware: one OVH b3-64 VM (16 vCPU, 64 GB), October 24, 2026.

Lokkal runs without long-term memory, without the judge model and without device approvals: an action it holds for
approval stays pending and the task goes on without it. The kernel configuration is `lokkal_config()` in
[run.py](run.py).

## Results

| Run | Harness | Resolved | Input tokens | Empty patches | Judge errors | Report |
|---|---|---|---|---|---|---|
| v50 | Nokker Lokkal, first run (auto mode, 80-call cap) | 28 / 50 (56 %) | 56.2 M | 4 | 1 | [v50.json](reports/v50.json) |
| v100 | Nokker Lokkal, sandbox-wide command permission, 100-call cap | 33 / 50 (66 %) | 82.8 M | 0 | 0 | [v100.json](reports/v100.json) |
| v3 | Nokker Lokkal, current `run.py` | **35 / 50 (70 %)** | **79.2 M** | 1 | 0 | [v3.json](reports/v3.json) |
| ref100 | Claude Code 2.1.310 driven by the same model (`run_ref.py`) | 37 / 50 (74 %) | 132.6 M | 0 | 0 | [ref100.json](reports/ref100.json) |

What changed between the Lokkal runs is described in [benchmarks.md](../../docs/en/benchmarks.md#what-changed-in-lokkal-between-runs).
On 50 tasks the 95 % interval is about ±13 points, and Lokkal reruns move by 2 to 3 tasks with no change: v3 and the
reference are on par.

Tasks on which v3 and the reference differ (from `resolved_ids` in [v3.json](reports/v3.json) and
[ref100.json](reports/ref100.json)):

| Resolved by the reference only | Resolved by Lokkal v3 only |
|---|---|
| django__django-11149 | django__django-16032 |
| django__django-15916 | django__django-16950 |
| pylint-dev__pylint-6386 | sphinx-doc__sphinx-11445 |
| scikit-learn__scikit-learn-14629 | |
| sympy__sympy-18211 | |

In v3, sphinx-doc__sphinx-8056 ended with an empty patch.

### Input tokens

Input tokens are the provider-reported prompt tokens summed over every model call of the 50 tasks. For Lokkal they
come from `nokker code --json` (one entry per call, as returned by the endpoint). For the reference run they were
summed from the upstream `prompt_tokens` of every request, logged by the translator. The provider has no prompt
cache, so every input token is billed; with a provider that caches the prompt prefix, the cost gap would be smaller
than the token gap.

`run_ref.py` records the usage that Claude Code reports at the end of each task. That figure is only as good as the
usage your translator passes back: if it does not forward input usage, the script records 0, and the provider's own
usage records are the ground truth.

## Reproduce

You need Docker with room for the 50 evaluation images, Python 3 with `pip install "swebench==4.1.0" datasets`,
and an API key for an OpenAI-compatible endpoint serving the model. Model spending is real: the published runs used
56 to 133 M input tokens each.

### Nokker Lokkal

`run.py` runs the Nokker code of this checkout (mounted read-only) inside the Lokkal image
`ghcr.io/nokker-ai/nokker:0.1.0-rc.5-lokkal` for its dependencies (`LOKKAL_IMAGE` overrides it). The endpoint comes
from `OPENAI_API_KEY` (required), `OPENAI_BASE_URL` and `OPENAI_MODEL` (default: the endpoint and model above), set in
the environment or in a file passed with `--env-file` (`KEY=VALUE` lines). With no key, `solve` stops with an error.

```sh
export OPENAI_API_KEY=...                       # or: --env-file ./endpoint.env
python3 bench/swebench/run.py solve --run v3 --parallel 3
python3 bench/swebench/run.py eval --run v3 --workers 8
python3 bench/swebench/run.py report --run v3
```

Options: `--only <id>,<id>` for a subset, `--max-calls` (100), `--max-input-tokens` (a spending guard over the whole
run, 90 M by default: once reached, the remaining tasks are not started). A task that already has a result is
skipped, so an interrupted run resumes. Work copies go to `SWEBENCH_WORK` (`/var/tmp/swebench`); predictions, per-task
results and the judge's report go to `bench/swebench/results/<run>/`.

### Reference: Claude Code driven by the same model

Claude Code speaks the Anthropic Messages API, so it needs a translator in front of the OpenAI-compatible endpoint.
The published reference run went through an Anthropic-to-OpenAI translator of ours that is not published. Any
compatible translator works; `run_ref.py` does not start one, it uses the one at `ANTHROPIC_BASE_URL`. For example,
with the [LiteLLM](https://github.com/BerriAI/litellm) proxy (not the one we used):

```yaml
# litellm.yaml
model_list:
  - model_name: claude-sonnet-5-5          # the name Claude Code sends (run_ref.py --client-model)
    litellm_params:
      model: openai/Qwen3.5-397B-A17B
      api_base: https://oai.endpoints.kepler.ai.cloud.ovh.net/v1
      api_key: os.environ/OPENAI_API_KEY
```

```sh
litellm --config litellm.yaml --port 4000 &
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export CC_BIN=/path/to/claude                   # the Claude Code standalone binary
python3 bench/swebench/run_ref.py solve --run ref100 --parallel 3
python3 bench/swebench/run.py eval --run ref100 --workers 8
python3 bench/swebench/run.py report --run ref100
```

Claude Code runs inside each evaluation image with `--network host`, so a translator on `127.0.0.1` is reachable.
`ANTHROPIC_API_KEY` is passed through if set (a placeholder otherwise). Web search, web fetch and scheduling tools
are disabled, and no MCP server is loaded.

## What is not here

The per-task traces and the predictions of the published runs are kept private. The reports in
[reports/](reports/) are the judge's verdict on those predictions; to check the numbers independently, rerun the
benchmark.
