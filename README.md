# KOT × FrontierHarness Eval reproducibility package

This repository contains the KOT harness implementation, the exact 30 task packages, the two canonical Harbor jobs, native KOT histories and telemetry, and the derived data submitted in [FrontierHarness Eval PR #9](https://github.com/frontier-harness-eval/eval/pull/9).

The public KOT release repository and product documentation are available at [Loqira-Labs/agentkot](https://github.com/Loqira-Labs/agentkot).

## Submitted result

| Metric | Value |
| --- | ---: |
| Tasks | 30 |
| Successful | 23 |
| Pass rate | 76.7% |
| Total measured cost | $65.0807388 |
| Effective cost per pass (`total cost / 23`) | $2.8295973 |
| Median successful-cell cost | $0.2775336 |
| Median successful-cell cache-hit rate | 94.1% |
| Median successful-cell agent time | 300.495 s |

Token totals from KOT's `/api/usage` endpoint:

| Token class | Count | Price used, USD / 1M |
| --- | ---: | ---: |
| Uncached input | 2,027,470 | $3.00 |
| Cache-read input | 144,773,846 | $0.30 |
| Cache-creation input | 0 | $3.00 |
| Output, including reasoning | 1,037,745 | $15.00 |
| Reasoning subset of output | 656,915 | — |

The cost formula is:

```text
(input × 3.00 + cache_read × 0.30 + cache_creation × 3.00 + output × 15.00) / 1,000,000
```

`$2.83` in the submitted comparison is `effective_cost_per_pass`: the total cost of all 30 attempts divided by 23 passes. The separate median cost of a successful cell is `$0.2775336`.

## Exact evaluated configuration

- **Benchmark:** FrontierHarness Eval v1
- **Tasks:** 21 Terminal-Bench 2.1 tasks and 9 DeepSWE tasks
- **Harness:** KOT 1.3.1
- **KOT source revision:** `96328165955504c601b67301d4be89bec5259b6b`
- **Executable:** `kot_harbor/kot`
- **Executable target:** `x86_64-unknown-linux-musl`
- **Executable SHA-256:** `ac992437515974b8dae5ac96ef78fc5d584d3bbf28d72dac1163e2d1ce2d558e`
- **Harbor:** 0.22.0 on Python 3.12.10
- **Model:** Kimi K3 through the Moonshot AI coding endpoint (`moonshotai/k3`)
- **Reasoning:** no explicit effort override; KOT resolves the K3 model default to `max`
- **Sampling:** `k=1`
- **Concurrency:** one task at a time (`n_concurrent=1`)
- **Agent network:** no general network; `api.kimi.com` is explicitly allowed by Harbor
- **Runs:** Terminal-Bench and DeepSWE are launched sequentially

The retained canonical jobs are:

```text
data/jobs/2026-09-04__00-24-47   # 21 Terminal-Bench tasks
data/jobs/2026-09-04__02-43-47   # 9 DeepSWE tasks
```

## Repository contents

```text
kot_harbor/
  kot                 exact musl-static KOT executable used for the run
  kot_agent.py        Harbor BaseInstalledAgent integration
  runner.py           in-container KOT HTTP/SSE driver
  finalize.py         timeout-path data-preserving finalizer

tasks/
  terminal-bench/     21 complete task packages
  deep-swe/           9 complete task packages

build/
  Dockerfile.bundle   derived image for build-cython-ext
  pyknotid.bundle     offline artifact referenced by that task

data/
  jobs/               raw Harbor jobs, trial results, verifier output and artifacts
  histories/          native KOT config homes, transcripts and telemetry for all 30 tasks
  history-index.json  task → job → trial → session → transcript mapping
  provenance.json     compact run and executable provenance
  redactions.json     exact record of credential-only redaction
  results/            KOT-only aggregate, raw metrics and task-source provenance
  submission/         benchmark metadata and integrated eval-data from PR #9

scripts/
  run.py              reproduce the 30-task run
  verify.py           rebuild all submitted metrics from raw data and histories
  print_history.py    render one native KOT transcript
```

## How the Harbor wrapper works

`KotAgent` is an installed Harbor agent. For each trial it:

1. Ensures `python3`, CA certificates and `procps` exist in the task container.
2. Uploads the exact `kot` executable, `runner.py`, `finalize.py`, and the task instruction to `/opt/kot`.
3. Detects the task image's actual working directory with `pwd` instead of assuming `/app`.
4. Starts the runner as the Harbor agent process and collects KOT usage into Harbor's token fields.
5. Runs the finalizer and process-group cleanup even when Harbor cancels the runner on timeout.

The in-container runner:

1. Creates a task-specific KOT config home under the host-mounted `kot-homes` directory.
2. Enables main-session auto-compaction and the evaluated retry configuration.
3. Starts `kot web` on `127.0.0.1:18080` with `--provider moonshotai --model k3 --workspace <task workdir> --new`.
4. Creates one KOT session through `POST /api/session`.
5. Opens the SSE stream before `POST /api/submit` and uses a stable `client_msg_id`.
6. Accepts completion only after a `Completed` terminal event, authoritative `Idle` state, an empty `/api/tasks` response, and a full quiet window.
7. Flushes KOT's virtual file layer through `POST /api/sync` before the verifier reads the task filesystem.
8. Captures cumulative task usage through `GET /api/usage`, closes the session, and terminates the KOT process group.

The launcher passes `KOT_BENCH_PROVIDER=moonshotai` and `KOT_BENCH_MODEL=k3`, so the source-level alternative credential route in the wrapper is not selected by this run.

## Reproduce the run

### Prerequisites

- Docker Engine or Docker Desktop
- Python 3.12
- a Moonshot AI coding-plan API key with access to Kimi K3
- network access for pulling/building the task images and for the Harbor verifier phases

Install the pinned Harbor version in an isolated environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set the credential without writing it into this repository:

```bash
export MOONSHOT_API_KEY='...'
```

```powershell
$env:MOONSHOT_API_KEY = '...'
```

Run both task families in the evaluated order:

```bash
python scripts/run.py
```

The script verifies Harbor 0.22.0 and the KOT executable hash, builds the required `kot-frontier/build-cython-ext:20251031` image, then issues two sequential Harbor runs with:

```text
--agent kot_harbor.kot_agent:KotAgent
-m moonshotai/k3
-k 1
--n-concurrent 1
--environment-build-timeout-multiplier 2
--allow-agent-host api.kimi.com
--ae KOT_BENCH_PROVIDER=moonshotai
--ae KOT_BENCH_MODEL=k3
--ae MOONSHOT_API_KEY=<value>
```

New output is written under `reproduction/<UTC timestamp>/`; the retained submission data under `data/` is never modified.

A single family can be run with:

```bash
python scripts/run.py --family terminal-bench
python scripts/run.py --family deep-swe
```

A single task can be run in a fresh isolated history directory with:

```bash
python scripts/run.py --task terminal-bench/code-from-image
```

## Verify the submitted data

```bash
python scripts/verify.py
```

The verifier checks:

- the exact KOT binary fingerprint and source/version provenance;
- 21 + 9 task-package coverage;
- 30 raw canonical trials and 30 matching KOT histories;
- task instructions against the first user message in every native transcript;
- job, trial and session identities across all data layers;
- verifier rewards, success flags, duration, turns and every token class;
- the full token totals, pricing formula, total cost and effective cost per pass;
- the KOT row embedded in the submitted `eval-data.json`;
- credential redaction and SHA-256 coverage of every retained file.

Expected final lines:

```text
PASS: FrontierHarness package is internally consistent
tasks=30 successes=23 jobs=2 histories=30
```

## Inspect a task history

Raw histories are line-delimited JSON under `data/histories/`. The index gives an exact path for every task:

```bash
python scripts/print_history.py terminal-bench/chess-best-move
python scripts/print_history.py datacurve/httpx-multipart-response-parsing --tail 20
```

Each history set includes the append-only KOT transcript, frozen request head, session metadata, settings, and any recorded LLM-failure telemetry.

## Task provenance

`data/results/provenance.json` records the materialized task sources.

- The 21 Terminal-Bench tasks come from the Harbor package cache for `terminal-bench/terminal-bench-2-1`. Their agent phase is `no-network`; the verifier phase is `public`. The task packages include their environments and verifiers.
- Six DeepSWE tasks use the recorded DeepSWE revision `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea`; three use tag `v1.0.0`, matching the FrontierHarness task definitions.
- `build-cython-ext` uses the local image `kot-frontier/build-cython-ext:20251031`, derived from `alexgshaw/build-cython-ext:20251031` by adding `build/pyknotid.bundle` at `/app/pyknotid.bundle`.
- `data/results/manifest-check.json` records a passing comparison of the 30 materialized instruction/task definitions against the FrontierHarness task set.

## Termination annotations

The submitted KOT row records two termination annotations while retaining the verifier outcomes:

- `terminal-bench/build-cython-ext`: agent timeout, verifier reward `0`;
- `datacurve/httpx-multipart-response-parsing`: the runner returned non-zero after its background-work quiescence cap, while the completed filesystem passed the verifier with reward `1`.

Both cases, including their exceptions, usage, verifier output and native histories, are present in `data/jobs/` and `data/histories/`.

## Credential redaction

The raw KOT histories contained one provider credential value because a task-side diagnostic command printed its environment. That single value is replaced with `[REDACTED_PROVIDER_CREDENTIAL]`. No instruction, model output, tool input, verifier artifact, score, token count, timestamp or other benchmark datum is changed. The affected path and replacement count are recorded in `data/redactions.json`.
