# delta_debugging: grade SRE diagnoses by experiment

delta debugging based evaluation takes the cause a diagnosis blames, undoes exactly that on a fresh copy of the broken app, and asks the
 SREGym mitigation oracle whether the app is fixed. If undoing the blamed cause fixes
the app, the diagnosis holds up. If it does not, the diagnosis is wrong, however well it reads.

The report puts the judge's score next to the experiment's verdict for every diagnosis, so the
two can be compared directly.

## Install

The tool runs from a SREGym checkout, because it uses SREGym to deploy apps, inject faults and run
the mitigation checks. Nothing in SREGym is changed.

```
git clone https://github.com/varuni7/delta_debugging.git ~/delta_debugging
cd <your SREGym checkout>
export PYTHONPATH=~/delta_debugging
uv run python -m delta_debugging --help
```

Copying the `delta_debugging/` folder to the root of the SREGym checkout works too. Tested against
SREGym main at commit `f25cdc1a`.

## What you need

- A SREGym checkout with its dependencies (`uv sync`).
- A **dedicated kind cluster** set up the usual SREGym way. The tool deploys, breaks and redeploys
  apps on it, so do not point it at a cluster you care about. It refuses any context that does not
  start with `kind-`, and it refuses to start if your current kubectl context is not the one you name.
- An API key for the translator model, set in your shell (never in a file in the repo). Any model
  LiteLLM supports works; the tool tells you which variable is missing before it starts.

  | Provider | `--model` | Key |
  | --- | --- | --- |
  | Gemini (default) | `gemini/gemini-3.1-pro-preview` | `GEMINI_API_KEY` |
  | Claude | `anthropic/claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
  | OpenAI | `openai/<model>` | `OPENAI_API_KEY` |
  | A proxy or OpenAI-compatible server | `openai/<model>` plus `--api-base <url>` | whatever the server needs |

  `BUTFOR_MODEL` sets the model once instead of passing `--model` every time.

## The three steps

All three share one working directory (`--out`). Each step can be re-run; finished work is kept.

### 1. translate: diagnoses to undo commands

```
export GEMINI_API_KEY=...   # or the key for the model you pick
uv run python -m delta_debugging translate results/<run_dir> [more dirs or CSVs] --out dd_out/ [--model ...]
```

Reads conductor results (`*_results.csv` with `Diagnosis.submission`) and SREGym-Lite trace sets
(`results.csv` plus trajectories). If your judge scores are in other columns, pass
`--score-col <column> --pass-col <column>`. `--problems a,b` limits the problems, and `--dry-run`
prints the prompts without calling the model.

The LLM only writes commands. It sees the diagnosis text and the app namespace, and nothing
else: not the answer key, not the cluster. If a diagnosis can be read as two different actions,
it returns both readings and both get tested. If it blames something that cannot be undone with
kubectl, the diagnosis is marked not translatable.

Output: `diagnoses.jsonl` and `translations.jsonl`. **Look at the translations before step 2.**
They are plain JSON lines. You can fix a bad translation by editing its `commands`, and step 2
runs whatever is in the file.

### 2. run: experiments on the cluster

```
kubectl config use-context kind-<name>
uv run python -m delta_debugging run --out dd_out/ --context kind-<name> --estimate   # trial count and hours
uv run python -m delta_debugging run --out dd_out/ --context kind-<name>
```

For each problem: deploy the app, run two controls (no change, and a restart of every
deployment), then test each distinct undo twice. An undo with several commands that works is
minimised with ddmin, to find the commands that matter. Identical undos from different diagnoses
are run once. A trial takes about 3 to 5 minutes. If the run stops, start it again with the same
command and it continues where it left off.

Options: `--problems a,b`, `--repeats N` (default 2), `--manifest-wait S` (default 90, seconds to
let the fault take effect), `--observe S` (default 180, seconds to wait for recovery),
`--keep-app` (do not undeploy afterwards).

Every command is checked before it runs: kubectl only, an allowed verb (delete, patch, set,
scale, label, annotate, apply, create, replace, rollout restart/undo/resume, and exec inside an
app namespace), no pipes or command chaining, nothing that deletes namespaces, nodes, CRDs or
cluster RBAC, nothing deleted from system namespaces, and every command pinned to `--context`.
Refused commands are listed in the report and never run.

Output: `trials/<problem>.jsonl` (one line per trial), `undos/<problem>.jsonl`,
`problems/<problem>.json` (deploy time, oracle, control results).

### 3. report

```
uv run python -m delta_debugging report --out dd_out/
```

Writes `report.csv` (one row per diagnosis) and `report.md`: agreement between judge and
experiment by judge score, agreement with whether the agent actually fixed the system (when the
results have it), and every disagreement with the diagnosis text and commands beside it.

## Reading the verdicts

| Verdict | Meaning |
| --- | --- |
| RECOVERS | Undoing what the diagnosis blames fixed the app in every repeat |
| DOES_NOT_RECOVER | It never fixed the app |
| MIXED | Repeats disagreed; treat as unknown |
| AMBIGUOUS | The diagnosis had readings with different results |
| NOT_TRANSLATABLE | It blames nothing that kubectl can undo, e.g. a component that does not exist |
| PROBLEM_UNUSABLE | The oracle passes even with no change, so nothing on this problem can be graded |
| UNSUPPORTED | The problem could not be built or deployed |

Two warnings worth knowing:

- If a plain restart passes a problem's oracle (shown in the Problems table), a passing undo on
  that problem may pass for the wrong reason.
- An undo that changes far more than needed still passes. The `minimal_commands` column shows the
  commands that mattered, so an over-broad undo is visible.

## Troubleshooting

- **A namespace hangs in Terminating during deploy** ("Namespace ... was not deleted within 300
  seconds"), and the kubelet log on a node shows `plugin type="calico" failed (delete): ...
  Unauthorized`. Calico's CNI token on that node has expired, which happens on kind clusters that
  have been up for days or through a laptop sleep. Restart Calico, then re-run the same command:
  `kubectl rollout restart daemonset/calico-node -n kube-system`.

## Tests

From this repo, with SREGym's environment: `<SREGym>/.venv/bin/python -m pytest tests` (no cluster or
API key needed).
