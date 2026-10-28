"""Run but-for trials for SREGym problems on a dedicated kind cluster."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from delta_debugging import safety


def _restart_base_traffic(problem) -> None:
    problem.workload.start()
    problem.workload.set_rate(problem.base_rate)
    time.sleep(problem.baseline_warmup_seconds)


# Setup the conductor does once per run but every trial needs.
AFTER_RESET: dict[str, Callable] = {
    "search_rate_retry_collapse_hotel_reservation": _restart_base_traffic
}


@dataclass
class Settings:
    context: str
    out_dir: Path
    repeats: int = 2
    manifest_wait_s: int = 90
    observe_s: int = 180
    healthy_timeout_s: int = 300
    keep_app: bool = False


def current_context() -> str:
    return subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    ).stdout.strip()


def require_context(context: str) -> None:
    # SREGym's own code uses the current context, so it must already be the target.
    if not context.startswith("kind-"):
        raise SystemExit(
            f"refusing to run on {context!r}: experiments only run on a dedicated kind cluster"
        )
    now = current_context()
    if now != context:
        raise SystemExit(
            f"current kubectl context is {now!r}, expected {context!r}; run: kubectl config use-context {context}"
        )


def run_command(cmd: str, context: str, timeout: int = 180) -> dict:
    why = safety.check(cmd)
    if why:
        return {"cmd": cmd, "rc": None, "refused": why}
    try:
        p = subprocess.run(
            safety.pin_context(cmd, context),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "rc": p.returncode,
            "stdout": p.stdout.strip()[-300:],
            "stderr": p.stderr.strip()[-300:],
        }
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": None, "stderr": f"timed out after {timeout}s"}


def _poll(
    pred: Callable[[], bool | None],
    want: bool,
    timeout_s: float,
    interval_s: float,
    stable: int = 1,
):
    start, streak, checks = time.monotonic(), 0, 0
    while True:
        checks += 1
        try:
            got = pred()
        except Exception:
            got = None
        streak = streak + 1 if got is want else 0
        if streak >= stable:
            return True, time.monotonic() - start, checks
        if time.monotonic() - start >= timeout_s:
            return False, time.monotonic() - start, checks
        time.sleep(interval_s)


def trial_key(commands: list[str], rep: int) -> str:
    return hashlib.sha1(json.dumps([commands, rep]).encode()).hexdigest()[:16]


class ProblemExperiments:
    def __init__(self, problem_id: str, settings: Settings):
        from sregym.conductor.oracles.mitigation import MitigationOracle
        from sregym.conductor.problems.registry import ProblemRegistry

        self.pid, self.s = problem_id, settings
        self.problem = ProblemRegistry().get_problem_instance(problem_id)
        self.ns = self.problem.namespace
        self.oracle = getattr(self.problem, "mitigation_oracle", None)
        if self.oracle is None:
            raise ValueError(f"{problem_id} has no mitigation oracle")
        self.oracle_name = type(self.oracle).__module__.rsplit(".", 1)[-1]
        self.health = MitigationOracle(self.problem)
        self.conductor = None
        trials_dir = settings.out_dir / "trials"
        trials_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = trials_dir / f"{problem_id}.jsonl"
        self.done: dict[str, dict] = {}
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                rec = json.loads(line)
                if rec["verdict"] != "INVALID":
                    self.done[rec["key"]] = rec

    def deploy(self) -> float:
        from sregym.conductor.conductor import Conductor, ConductorConfig

        start = time.monotonic()
        c = Conductor(config=ConductorConfig(deploy_loki=False))
        c.problem_id, c.problem, c.app = self.pid, self.problem, self.problem.app
        c.dependency_check(["kubectl", "helm", "docker"])
        c.fix_kubernetes()
        c.undeploy_app()
        c.deploy_app()
        self.conductor = c
        self.health.capture_baseline()
        if hasattr(self.oracle, "capture_baseline"):
            self.oracle.capture_baseline()
        return time.monotonic() - start

    def undeploy(self) -> None:
        if self.conductor and not self.s.keep_app:
            self.conductor.undeploy_app()

    def healthy(self) -> bool | None:
        return self.health._evaluate_current_state().get("success") is True

    def settled(self) -> bool | None:
        """healthy(), after waiting for in-progress rollouts."""
        return self.health.evaluate().get("success") is True

    def reset(self) -> tuple[bool, float, str]:
        start = time.monotonic()
        try:
            self.problem.recover_fault()
        except Exception as e:  # fine when the undo already removed part of it
            print(f"    [reset] recover_fault raised {type(e).__name__}: {e}")
        for flag in (
            "_injection_attempted",
            "fault_injected",
        ):  # some problems refuse a second injection
            if hasattr(self.problem, flag):
                setattr(self.problem, flag, False)
        if self.pid in AFTER_RESET:
            AFTER_RESET[self.pid](self.problem)
        ok, _, _ = _poll(
            self.settled, True, self.s.healthy_timeout_s, interval_s=10, stable=2
        )
        note = "healthy"
        if not ok:
            print("    [reset] app did not recover; redeploying")
            self.deploy()
            ok, _, _ = _poll(
                self.settled, True, self.s.healthy_timeout_s, interval_s=10, stable=2
            )
            note = "redeployed" if ok else "unhealthy after redeploy"
        return ok, time.monotonic() - start, note

    def evaluate(self) -> dict:
        try:
            return self.oracle.evaluate() or {}
        except Exception as e:
            return {"success": None, "error": f"{type(e).__name__}: {e}"}

    def trial(self, commands: list[str], kind: str, rep: int = 1) -> dict:
        key = trial_key(commands, rep)
        if key in self.done:
            return self.done[key]
        rec = {
            "problem": self.pid,
            "key": key,
            "kind": kind,
            "commands": commands,
            "rep": rep,
            "oracle": self.oracle_name,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phases_s": {},
        }
        print(
            f"  [{time.strftime('%H:%M:%S')}] {self.pid} {kind} rep {rep}: {len(commands)} command(s)"
        )
        try:
            ok, secs, note = self.reset()
            rec["phases_s"]["reset"], rec["reset"] = round(secs, 1), note
            if not ok:
                return self._finish(rec, "INVALID", "app not healthy before injection")

            t = time.monotonic()
            self.problem.inject_fault()
            visible, _, _ = _poll(
                self.healthy, False, self.s.manifest_wait_s, interval_s=5, stable=2
            )
            if not visible:  # invisible to the health check
                time.sleep(max(0.0, self.s.manifest_wait_s - (time.monotonic() - t)))
            rec["fault_visible_to_health_check"] = visible
            rec["phases_s"]["inject"] = round(time.monotonic() - t, 1)

            t = time.monotonic()
            rec["command_results"] = [run_command(c, self.s.context) for c in commands]
            rec["commands_failed"] = sum(
                r.get("rc") != 0 for r in rec["command_results"]
            )
            rec["phases_s"]["apply"] = round(time.monotonic() - t, 1)

            t = time.monotonic()
            holder = {}

            def passes():
                holder["r"] = self.evaluate()
                return holder["r"].get("success") is True

            recovered, secs, checks = _poll(
                passes, True, self.s.observe_s, interval_s=15
            )
            result = holder.get("r", {})
            rec["phases_s"]["judge"] = round(time.monotonic() - t, 1)
            rec["oracle_calls"] = checks
            rec["time_to_recover_s"] = round(secs, 1) if recovered else None
            rec["oracle_result"] = json.loads(json.dumps(result, default=str))
            if result.get("success") is None and "error" in result:
                return self._finish(rec, "INVALID", f"oracle error: {result['error']}")
            return self._finish(
                rec,
                "RECOVERED" if result.get("success") is True else "NOT_RECOVERED",
                "",
            )
        except Exception as e:
            rec["error"] = traceback.format_exc()[-1500:]
            return self._finish(
                rec, "INVALID", f"harness error: {type(e).__name__}: {e}"
            )

    def _finish(self, rec: dict, verdict: str, detail: str) -> dict:
        rec["verdict"], rec["detail"] = verdict, detail
        rec["total_s"] = round(sum(rec["phases_s"].values()), 1)
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        if verdict != "INVALID":
            self.done[rec["key"]] = rec
        print(f"    -> {verdict} in {rec['total_s']}s {detail}")
        return rec

    def recovers(self, commands: list[str], kind: str, rep: int = 1) -> bool | None:
        rec = self.trial(commands, kind, rep)
        if rec["verdict"] == "INVALID":
            rec = self.trial(commands, kind, rep)
        return None if rec["verdict"] == "INVALID" else rec["verdict"] == "RECOVERED"

    def controls(self) -> dict:
        noop = self.recovers([], "control:noop")
        placebo = self.recovers(
            [f"kubectl rollout restart deployment -n {self.ns}"], "control:placebo"
        )
        status = "ok"
        if noop is None:
            status = "unusable: the no-change control could not be set up"
        elif noop:
            status = "unusable: the mitigation oracle passes the broken system"
        return {"noop_recovers": noop, "placebo_recovers": placebo, "status": status}

    def ddmin(self, commands: list[str]) -> list[str] | None:
        """Zeller's ddmin, keeping the commands in their original order."""
        c, n = list(range(len(commands))), 2

        def test(idx):
            return self.recovers([commands[i] for i in sorted(idx)], "subset")

        while len(c) >= 2:
            size = len(c) // n
            chunks = [
                c[i * size : (i + 1) * size] if i < n - 1 else c[i * size :]
                for i in range(n)
            ]
            reduced = False
            for ch in chunks:
                if test(ch):
                    c, n, reduced = ch, 2, True
                    break
            if not reduced:
                for ch in chunks:
                    comp = [i for i in c if i not in ch]
                    if comp and test(comp):
                        c, n, reduced = comp, max(n - 1, 2), True
                        break
            if not reduced:
                if n >= len(c):
                    break
                n = min(len(c), 2 * n)
        return [commands[i] for i in sorted(c)]

    def test_undo(self, commands: list[str]) -> dict:
        verdicts = [
            self.recovers(commands, "undo", rep) for rep in range(1, self.s.repeats + 1)
        ]
        out = {"commands": commands, "recovers": verdicts, "minimal": None}
        if verdicts and verdicts[0] and len(commands) > 1:
            out["minimal"] = self.ddmin(commands)
        return out


def estimate(
    undos_by_problem: dict[str, list[list[str]]],
    repeats: int,
    minutes_per_trial: float = 4.0,
) -> dict:
    trials = 0
    for undos in undos_by_problem.values():
        trials += 2  # controls
        for cmds in undos:
            trials += repeats + (2 * len(cmds) if len(cmds) > 1 else 0)
    deploy_min = 5 * len(undos_by_problem)
    hours = (trials * minutes_per_trial + deploy_min) / 60
    return {
        "problems": len(undos_by_problem),
        "distinct_undos": sum(map(len, undos_by_problem.values())),
        "trials_upper_estimate": trials,
        "hours_estimate": round(hours, 1),
    }
