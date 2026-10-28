"""Command line: ``uv run python -m delta_debugging {translate,run,report} ...``. See README.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from delta_debugging import engine, report
from delta_debugging.results import read_diagnoses
from delta_debugging.translate import DEFAULT_MODEL, missing_keys, translate


def _problems(arg: str | None) -> set[str] | None:
    return {p.strip() for p in arg.split(",") if p.strip()} if arg else None


def cmd_translate(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    diagnoses = read_diagnoses(a.results, a.score_col, a.pass_col)
    keep = _problems(a.problems)
    if keep:
        diagnoses = [d for d in diagnoses if d.problem_id in keep]
    if not diagnoses:
        sys.exit("no diagnoses found under " + ", ".join(a.results))
    path = out / "diagnoses.jsonl"
    known = {}
    if path.exists():
        known = {
            json.loads(line)["id"]: json.loads(line)
            for line in path.read_text().splitlines()
        }
    known.update({d.id: d.to_dict() for d in diagnoses})
    path.write_text("".join(json.dumps(d) + "\n" for d in known.values()))
    print(
        f"{len(diagnoses)} diagnoses from {len({d.problem_id for d in diagnoses})} problems"
    )
    model = a.model or os.environ.get("BUTFOR_MODEL") or DEFAULT_MODEL
    missing = [] if a.dry_run or a.api_base else missing_keys(model)
    if missing:
        sys.exit(
            f"{model} needs {', '.join(missing)} in the environment (or pick another model with --model)"
        )
    translate(
        diagnoses,
        model,
        out / "translations.jsonl",
        api_base=a.api_base,
        dry_run=a.dry_run,
    )


def _undos(out: Path, keep: set[str] | None) -> dict[str, list[list[str]]]:
    by_problem: dict[str, list[list[str]]] = {}
    path = out / "translations.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found; run translate first")
    for line in path.read_text().splitlines():
        t = json.loads(line)
        if not t.get("executable") or (keep and t["problem_id"] not in keep):
            continue
        undos = by_problem.setdefault(t["problem_id"], [])
        for r in t["readings"]:
            if r["commands"] and not r.get("rejected") and r["commands"] not in undos:
                undos.append(r["commands"])
    return by_problem


def cmd_run(a) -> None:
    out = Path(a.out)
    undos_by_problem = _undos(out, _problems(a.problems))
    est = engine.estimate(undos_by_problem, a.repeats)
    print(json.dumps(est, indent=1))
    if a.estimate:
        return
    engine.require_context(a.context)
    settings = engine.Settings(
        context=a.context,
        out_dir=out,
        repeats=a.repeats,
        manifest_wait_s=a.manifest_wait,
        observe_s=a.observe,
        keep_app=a.keep_app,
    )
    (out / "problems").mkdir(exist_ok=True)
    (out / "undos").mkdir(exist_ok=True)
    for pid, undos in sorted(undos_by_problem.items()):
        status_path = out / "problems" / f"{pid}.json"
        print(f"\n=== {pid}: {len(undos)} distinct undo(s)")
        try:
            exp = engine.ProblemExperiments(pid, settings)
        except Exception as e:
            status_path.write_text(
                json.dumps({"status": f"unsupported: {type(e).__name__}: {e}"})
            )
            print(f"    unsupported: {e}")
            continue
        status = {"namespace": exp.ns, "oracle": exp.oracle_name}
        try:
            status["deploy_s"] = round(exp.deploy(), 1)
            status.update(exp.controls())
            status_path.write_text(json.dumps(status, indent=1))
            if status["status"] != "ok":
                print(f"    skipped: {status['status']}")
                continue
            with (out / "undos" / f"{pid}.jsonl").open("a") as f:
                for cmds in undos:
                    f.write(json.dumps(exp.test_undo(cmds)) + "\n")
                    f.flush()
            exp.reset()
        except Exception as e:
            status["status"] = f"unsupported: {type(e).__name__}: {e}"
            status_path.write_text(json.dumps(status, indent=1))
            print(f"    failed: {e}")
        finally:
            exp.undeploy()
    print(f"\nreport: uv run python -m delta_debugging report --out {out}")


def cmd_report(a) -> None:
    path = report.write(Path(a.out), a.repeats)
    print(path.read_text())
    print(f"written: {path} and {path.with_suffix('.csv')}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="delta_debugging", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("translate", help="diagnoses -> undo commands (LLM)")
    t.add_argument("results", nargs="+", help="SREGym results directories or CSV files")
    t.add_argument(
        "--out", required=True, help="working directory shared by all three steps"
    )
    t.add_argument(
        "--model",
        help=f"LiteLLM model name; defaults to $BUTFOR_MODEL, then {DEFAULT_MODEL}",
    )
    t.add_argument("--problems", help="comma-separated problem ids to keep")
    t.add_argument(
        "--score-col", default="", help="CSV column with the judge's score (0-100)"
    )
    t.add_argument(
        "--pass-col", default="", help="CSV column with the judge's pass/fail"
    )
    t.add_argument(
        "--api-base", help="endpoint for a proxy or OpenAI-compatible server"
    )
    t.add_argument(
        "--dry-run",
        action="store_true",
        help="print the prompts without calling the model",
    )
    t.set_defaults(fn=cmd_translate)

    r = sub.add_parser("run", help="undo commands -> experiments on a kind cluster")
    r.add_argument("--out", required=True)
    r.add_argument(
        "--context",
        default="kind-kind",
        help="kubectl context of the dedicated kind cluster",
    )
    r.add_argument("--problems", help="comma-separated problem ids to run")
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument(
        "--manifest-wait",
        type=int,
        default=90,
        help="seconds to let the fault take effect",
    )
    r.add_argument(
        "--observe", type=int, default=180, help="seconds to wait for recovery"
    )
    r.add_argument(
        "--keep-app", action="store_true", help="leave each app deployed afterwards"
    )
    r.add_argument(
        "--estimate",
        action="store_true",
        help="print the trial count and time, then stop",
    )
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser(
        "report", help="experiments + judge scores -> report.csv and report.md"
    )
    s.add_argument("--out", required=True)
    s.add_argument("--repeats", type=int, default=2)
    s.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
