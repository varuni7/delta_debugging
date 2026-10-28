"""Read diagnoses and judge verdicts from SREGym conductor results and SREGym-Lite trace sets."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_SOLUTION_JSON = re.compile(r'"solution"\s*:\s*"((?:[^"\\]|\\.)*)"')
_SOLUTION_SHELL_VAR = re.compile(r"\bsolution=(['\"])(.*?)(?<!\\)\1", re.S)


@dataclass
class Diagnosis:
    id: str
    problem_id: str
    agent: str
    run: str
    text: str
    judge_score: float | None
    judge_pass: bool | None
    mitigation_pass: bool | None
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


def _bool(v) -> bool | None:
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("true", "1", "yes", "pass")


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def solution_from_command(cmd: str) -> str | None:
    """Find the posted solution at any quoting depth, peeling one backslash layer at a time."""
    text = cmd
    for _ in range(4):
        m = _SOLUTION_JSON.search(text)
        if m:
            try:
                value = json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                value = m.group(1)
            if value.strip():
                return value.strip()
        text = re.sub(r"\\(.)", r"\1", text, flags=re.S)
    m = _SOLUTION_SHELL_VAR.search(cmd)
    return m.group(2).strip() if m and m.group(2).strip() else None


def submitted_solution(trajectory: dict) -> str | None:
    """Return the diagnosis an agent posted to /submit, from an ATIF trajectory."""
    step_id = ((trajectory.get("extra") or {}).get("sregym") or {}).get(
        "diagnosis_submitted_step"
    )
    steps = trajectory.get("steps") or []
    candidates = (
        [s for s in steps if s.get("step_id") == step_id]
        if step_id is not None
        else steps
    )
    for step in candidates:
        for call in step.get("tool_calls") or []:
            for s in _strings(call.get("arguments") or {}):
                if "submit" in s and "solution" in s:
                    found = solution_from_command(s)
                    if found:
                        return found
    return None


def _read_conductor_csv(path: Path, score_col: str, pass_col: str) -> list[Diagnosis]:
    out = []
    agent, run_dir = path.parent.parent.name, path.parent.parent.parent.name
    for row in csv.DictReader(path.open()):
        pid = row.get("problem_id") or path.parent.name
        run = f"{run_dir}-{row.get('attempt') or '1'}"
        out.append(
            Diagnosis(
                id=f"{agent}/{pid}/{run}",
                problem_id=pid,
                agent=agent,
                run=str(run),
                text=(row.get("Diagnosis.submission") or "").strip(),
                judge_score=_float(row.get(score_col or "Diagnosis.accuracy")),
                judge_pass=_bool(row.get(pass_col or "Diagnosis.success")),
                mitigation_pass=_bool(row.get("Mitigation.success")),
                source=str(path),
            )
        )
    return out


def _read_lite_csv(path: Path, score_col: str, pass_col: str) -> list[Diagnosis]:
    out = []
    for row in csv.DictReader(path.open()):
        agent = (
            "_".join(x for x in (row.get("model"), row.get("effort")) if x)
            or path.parent.name
        )
        traj_path = path.parent / row["trajectory"]
        text = None
        if traj_path.exists():
            try:
                text = submitted_solution(json.loads(traj_path.read_text()))
            except (json.JSONDecodeError, OSError):
                text = None
        run = re.sub(r"\D", "", traj_path.stem) or "1"
        out.append(
            Diagnosis(
                id=f"{agent}/{row['problem_id']}/{run}",
                problem_id=row["problem_id"],
                agent=agent,
                run=run,
                text=text or "",
                judge_score=_float(row.get(score_col or "diag_accuracy")),
                judge_pass=_bool(row.get(pass_col or "diag_success")),
                mitigation_pass=_bool(row.get("mitigation_success")),
                source=str(traj_path),
            )
        )
    return out


def read_diagnoses(
    paths: list[str], score_col: str = "", pass_col: str = ""
) -> list[Diagnosis]:
    found: dict[str, Diagnosis] = {}
    for p in map(Path, paths):
        csvs = [p] if p.is_file() else sorted(p.rglob("*.csv"))
        for f in csvs:
            # Per-attempt copies and run roll-ups repeat the per-problem rows.
            if f.parent.name.startswith("run_") or f.name.endswith("_ALL_results.csv"):
                continue
            try:
                header = next(csv.reader(f.open()), [])
            except (OSError, UnicodeDecodeError):
                continue
            if "Diagnosis.submission" in header:
                rows = _read_conductor_csv(f, score_col, pass_col)
            elif "trajectory" in header and "problem_id" in header:
                rows = _read_lite_csv(f, score_col, pass_col)
            else:
                continue
            for d in rows:
                found[d.id] = d
    return sorted(found.values(), key=lambda d: (d.problem_id, d.agent, d.run))
