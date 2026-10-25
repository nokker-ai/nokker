# Benchmarks

## SWE-bench Verified (50-task sample), October 2026

**Nokker Lokkal resolves 35 of 50 tasks (70 %), at the level of Claude Code driven by the same model (37/50,
74 %), with 40 % fewer input tokens.**

| Harness | Model | Resolved | Input tokens (50 tasks) | Per task |
|---|---|---|---|---|
| Nokker Lokkal | Qwen3.5-397B-A17B | 35 / 50 (70 %) | 79.2 M | 1.6 M |
| Claude Code 2.1.310 | Qwen3.5-397B-A17B (same endpoint) | 37 / 50 (74 %) | 132.6 M | 2.7 M |
| Qwen's published result (its own harness) | Qwen3.5-397B-A17B | 76.4 % (500 tasks) | — | — |

Read this carefully:

- **Same model on both sides.** Claude Code was not run with a Claude model: it was pointed at the same
  Qwen3.5-397B-A17B endpoint (OVH AI Endpoints) through an Anthropic-to-OpenAI translation proxy (ours, not
  published; any compatible proxy works, see the [benchmark README](../../bench/swebench/README.md)). The comparison
  measures the harness, not the model.
- **"At the level of", not "better than".** On 50 tasks a 2-task gap is within noise (the 95 % interval is about
  ±13 points, and our own reruns move by 2–3 tasks without any change). 35 vs 37 says the two harnesses are on par.
  Both land close to the model's published 76.4 %.
- **Tokens, not euros.** The provider has no prompt cache, so every input token is billed. With a provider that
  caches the prompt prefix (Claude Code is built for Anthropic's cache), the cost gap would be smaller than the
  token gap.

### Method

- Tasks: 50 instances drawn once from `princeton-nlp/SWE-bench_Verified` (test split, 500 tasks) with
  `random.Random(20261025).sample(sorted(ids), 50)`, never edited by hand.
- Judging: the official SWE-bench harness (`swebench` 4.1.0, `run_evaluation`), official evaluation images, hidden
  tests. A task counts as resolved only if its FAIL_TO_PASS tests pass and no PASS_TO_PASS test breaks.
- Same conditions for both harnesses: one attempt per task, no retries, the same task prompt, at most 100 model
  calls (`--max-calls` / `--max-turns`), a 40-minute timeout, the official evaluation image of the task as the
  sandbox (the repository mounted at `/testbed`, its conda environment), all commands allowed inside that
  disposable sandbox, no network tools, no web search.
- Model settings: provider defaults (thinking enabled), 16 384 output tokens per call.
- Hardware: one OVH b3-64 VM (16 vCPU, 64 GB), October 24, 2026.

### What changed in Lokkal between runs

We publish the runs we made, not only the best one:

| Run | Resolved | Input tokens | What changed |
|---|---|---|---|
| 1 | 28 / 50 | 56.2 M | first run: auto mode held "destructive" commands for an approval nobody could give, 80-call cap |
| 2 | 33 / 50 | 82.8 M | sandbox-wide command permission (as the reference), 100-call cap, budget reminder, stall detection |
| 3 | 35 / 50 | 79.2 M | secret filter no longer masks paths like `/opt/miniconda3/bin/activate`; reproduce the bug before fixing, run the existing tests before concluding |

### Reproduce

The sample, the scripts for both harnesses and the official judge's report of every run above are in
[bench/swebench/](../../bench/swebench/); its [README](../../bench/swebench/README.md) gives the commands, the
conditions and the tasks on which Lokkal and the reference differ.

### Next

The full 500 tasks, with the tier routing and the reviewer described in [models.md](models.md) switched on. This
sample is a checkpoint, not a leaderboard entry.
