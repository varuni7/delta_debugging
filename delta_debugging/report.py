"""Put each diagnosis's judge score next to what the experiments found."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

TESTED = {"RECOVERS", "DOES_NOT_RECOVER"}


def _jsonl(path: Path) -> list[dict]:
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def load(out_dir: Path) -> dict:
    trials: dict[str, dict[tuple, list[dict]]] = {}
    for f in (out_dir / "trials").glob("*.jsonl"):
        by_cmds: dict[tuple, list[dict]] = {}
        for rec in _jsonl(f):
            if rec["verdict"] != "INVALID" and rec["kind"] in ("undo", "subset"):
                by_cmds.setdefault(tuple(rec["commands"]), []).append(rec)
        trials[f.stem] = by_cmds
    problems = {
        f.stem: json.loads(f.read_text()) for f in (out_dir / "problems").glob("*.json")
    }
    undos = {}
    for f in (out_dir / "undos").glob("*.jsonl"):
        for rec in _jsonl(f):
            undos[(f.stem, tuple(rec["commands"]))] = rec
    return {
        "diagnoses": {d["id"]: d for d in _jsonl(out_dir / "diagnoses.jsonl")},
        "translations": {
            t["diagnosis_id"]: t for t in _jsonl(out_dir / "translations.jsonl")
        },
        "trials": trials,
        "problems": problems,
        "undos": undos,
    }


def reading_verdict(pid: str, reading: dict, data: dict, repeats: int) -> dict:
    if reading.get("rejected"):
        return {
            "verdict": "REFUSED",
            "why": "; ".join(f"{c}: {w}" for c, w in reading["rejected"].items()),
        }
    cmds = tuple(reading["commands"])
    recs = [
        r for r in data["trials"].get(pid, {}).get(cmds, []) if r["kind"] == "undo"
    ][:repeats]
    undo = data["undos"].get((pid, cmds), {})
    out = {
        "runs": len(recs),
        "minimal": undo.get("minimal"),
        "commands_failed": max((r.get("commands_failed", 0) for r in recs), default=0),
    }
    got = [r["verdict"] == "RECOVERED" for r in recs]
    if not got:
        out["verdict"] = "NOT_RUN"
    elif all(got):
        out["verdict"] = "RECOVERS"
    elif not any(got):
        out["verdict"] = "DOES_NOT_RECOVER"
    else:
        out["verdict"] = "MIXED"
    return out


def diagnosis_rows(data: dict, repeats: int) -> list[dict]:
    rows = []
    for did, d in sorted(data["diagnoses"].items()):
        t = data["translations"].get(did)
        pstat = data["problems"].get(d["problem_id"], {}).get("status")
        row = {
            "diagnosis_id": did,
            "problem_id": d["problem_id"],
            "agent": d["agent"],
            "judge_score": d["judge_score"],
            "judge_pass": d["judge_pass"],
            "mitigation_pass": d["mitigation_pass"],
            "readings": [],
            "note": "",
        }
        if not d["text"]:
            row["experiment"] = "NO_DIAGNOSIS"
        elif t is None:
            row["experiment"] = "NOT_TRANSLATED"
        elif not t.get("executable"):
            row["experiment"], row["note"] = "NOT_TRANSLATABLE", t.get("note", "")
        elif pstat is None:
            row["experiment"] = "NOT_RUN"
        elif pstat.startswith("unsupported"):
            row["experiment"], row["note"] = "UNSUPPORTED", pstat
        elif pstat.startswith("unusable"):
            row["experiment"], row["note"] = "PROBLEM_UNUSABLE", pstat
        else:
            for r in t["readings"]:
                row["readings"].append(
                    {**r, **reading_verdict(d["problem_id"], r, data, repeats)}
                )
            verdicts = {r["verdict"] for r in row["readings"]}
            row["experiment"] = verdicts.pop() if len(verdicts) == 1 else "AMBIGUOUS"
        rows.append(row)
    return rows


def _band(score) -> str:
    if score is None:
        return "no score"
    return "0 or 100" if score in (0, 100) else "between"


def _agree(a, b) -> bool | None:
    return None if a is None or b is None else a == b


def write(out_dir: Path, repeats: int = 2) -> Path:
    data = load(out_dir)
    rows = diagnosis_rows(data, repeats)
    with (out_dir / "report.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "diagnosis_id",
                "problem_id",
                "agent",
                "judge_score",
                "judge_pass",
                "experiment",
                "mitigation_pass",
                "readings",
                "minimal_commands",
                "note",
            ]
        )
        for r in rows:
            readings = " || ".join(
                f"{x['id']}:{x['verdict']}: " + " ; ".join(x["commands"])
                for x in r["readings"]
            )
            minimal = " || ".join(
                " ; ".join(x["minimal"]) for x in r["readings"] if x.get("minimal")
            )
            w.writerow(
                [
                    r["diagnosis_id"],
                    r["problem_id"],
                    r["agent"],
                    r["judge_score"],
                    r["judge_pass"],
                    r["experiment"],
                    r["mitigation_pass"],
                    readings,
                    minimal,
                    r["note"],
                ]
            )

    lines = ["# But-for report", ""]
    counts = Counter(r["experiment"] for r in rows)
    lines += [
        f"{len(rows)} diagnoses. Experiment verdicts: "
        + ", ".join(f"{k} {v}" for k, v in counts.most_common())
        + ".",
        "",
    ]

    lines += [
        "## Problems",
        "",
        "| Problem | Oracle | Status | No change recovers | Restart recovers |",
        "| --- | --- | --- | --- | --- |",
    ]
    for pid, p in sorted(data["problems"].items()):
        lines.append(
            f"| {pid} | {p.get('oracle', '')} | {p.get('status', '')} | {p.get('noop_recovers')} | "
            f"{p.get('placebo_recovers')} |"
        )
    flagged = [pid for pid, p in data["problems"].items() if p.get("placebo_recovers")]
    if flagged:
        lines += [
            "",
            "A plain restart passes the oracle on: "
            + ", ".join(sorted(flagged))
            + ". On these problems a passing undo may be passing for the wrong reason.",
        ]

    tested = [r for r in rows if r["experiment"] in TESTED]
    lines += [
        "",
        "## Judge versus experiment",
        "",
        f"{len(tested)} diagnoses have a clear experiment verdict.",
        "",
        "| Judge score | Diagnoses | Judge agrees with experiment |",
        "| --- | --- | --- |",
    ]
    for band in ("0 or 100", "between", "no score"):
        sub = [r for r in tested if _band(r["judge_score"]) == band]
        agree = [_agree(r["judge_pass"], r["experiment"] == "RECOVERS") for r in sub]
        known = [a for a in agree if a is not None]
        if sub:
            lines.append(f"| {band} | {len(sub)} | {sum(known)} of {len(known)} |")

    with_mit = [r for r in tested if r["mitigation_pass"] is not None]
    if with_mit:
        exp = sum(
            _agree(r["experiment"] == "RECOVERS", r["mitigation_pass"])
            for r in with_mit
        )
        judge = sum(
            bool(_agree(r["judge_pass"], r["mitigation_pass"])) for r in with_mit
        )
        lines += [
            "",
            f"Against whether the agent actually fixed the system in its own run ({len(with_mit)} "
            f"diagnoses): the experiment matches {exp}, the judge matches {judge}.",
        ]

    def detail(r):
        out = [
            f"### {r['diagnosis_id']}",
            "",
            f"Judge {r['judge_score']} ({'pass' if r['judge_pass'] else 'fail'}), experiment {r['experiment']}.",
            "",
            "> "
            + data["diagnoses"][r["diagnosis_id"]]["text"][:600].replace("\n", " "),
            "",
        ]
        for x in r["readings"]:
            out.append(f"- Reading {x['id']} ({x['verdict']}): {x.get('summary', '')}")
            out += [f"    - `{c}`" for c in x["commands"]]
            if x.get("minimal") and x["minimal"] != x["commands"]:
                out.append(
                    "    - smallest set that still recovers: "
                    + "; ".join(f"`{c}`" for c in x["minimal"])
                )
        return out + [""]

    disagree = [
        r
        for r in tested
        if _agree(r["judge_pass"], r["experiment"] == "RECOVERS") is False
    ]
    lines += ["", f"## Judge and experiment disagree ({len(disagree)})", ""]
    for r in disagree:
        lines += detail(r)
    ambiguous = [r for r in rows if r["experiment"] in ("AMBIGUOUS", "MIXED")]
    if ambiguous:
        lines += [f"## Ambiguous or unstable ({len(ambiguous)})", ""]
        for r in ambiguous:
            lines += detail(r)
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path
