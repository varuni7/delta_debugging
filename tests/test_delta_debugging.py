import json

import pytest

from delta_debugging import engine, report, safety
from delta_debugging.results import solution_from_command
from delta_debugging.translate import missing_keys, parse_response


@pytest.mark.parametrize(
    "cmd",
    [
        """curl -X POST http://h:8000/submit --data-raw '{"solution":"The NetworkPolicy blocks it."}'""",
        """const r = await tools.exec_command({"cmd":"curl -X POST http://h:8000/submit --data '{\\\\\\"solution\\\\\\":\\\\\\"The NetworkPolicy blocks it.\\\\\\"}'"});""",
        """solution='The NetworkPolicy blocks it.'; jq -n --arg solution "$solution" '{solution:$solution}' | curl --data-binary @- http://h:8000/submit""",
    ],
)
def test_solution_is_found_at_any_quoting_depth(cmd):
    assert solution_from_command(cmd) == "The NetworkPolicy blocks it."


def test_solution_keeps_escaped_quotes_and_apostrophes():
    cmd = r"""curl --data '{"solution":"named \"deny-all\" and the Deployment's limit"}' http://h/submit"""
    assert solution_from_command(cmd) == 'named "deny-all" and the Deployment\'s limit'


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl delete networkpolicy deny-all-recommendation -n hotel-reservation",
        "kubectl set selector svc/frontend -n hotel-reservation io.kompose.service=frontend",
        "kubectl patch deployment search -n hotel-reservation --type=json "
        '-p=\'[{"op": "remove", "path": "/spec/template/metadata/labels/service-route"}]\'',
        "kubectl delete mutatingwebhookconfiguration gatekeeper-mutating-webhook-configuration",
        "kubectl rollout restart deployment -n social-network",
        'kubectl exec -n hotel-reservation deploy/mongodb-geo -- mongo admin --eval \'db.grantRolesToUser("admin", [{role: "readWrite", db: "geo-db"}]); db.x()\'',
        "kubectl -n hotel-reservation delete networkpolicy deny-all-recommendation",
        "kubectl apply -f - <<'EOF'\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: a}\nEOF",
    ],
)
def test_safe_commands_pass(cmd):
    assert safety.check(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl delete namespace hotel-reservation",
        "kubectl delete ns/hotel-reservation",
        "kubectl delete deployment coredns -n kube-system",
        "kubectl get pods -n x | xargs kubectl delete pod",
        "kubectl delete pod a -n x && kubectl delete pod b -n x",
        "kubectl exec -n kube-system deploy/coredns -- sh",
        "kubectl exec deploy/a -- mongo",
        "kubectl exec -n x deploy/a",
        "kubectl delete pods --all -A",
        "kubectl --context kind-prod delete deployment a -n x",
        "kubectl rollout pause deployment a -n x",
        "rm -rf /",
        "kubectl apply -f - <<EOF\napiVersion: v1\nkind: Namespace\nmetadata: {name: x}\nEOF",
    ],
)
def test_unsafe_commands_are_refused(cmd):
    assert safety.check(cmd) is not None


def test_context_is_pinned():
    assert safety.pin_context("kubectl delete svc a -n x", "kind-delta_debugging") == (
        "kubectl --context kind-delta_debugging delete svc a -n x"
    )


def test_parse_response_keeps_readings_and_flags_refused_commands():
    raw = """```json
{"executable": true, "note": "", "readings": [
 {"summary": "delete the webhook configuration", "inferred": false,
  "commands": ["kubectl delete mutatingwebhookconfiguration gk"]},
 {"summary": "delete the controller", "commands": ["kubectl delete deploy ctl -n platform-ops",
  "kubectl delete namespace platform-ops"]}]}
```"""
    out = parse_response(raw)
    assert out["executable"] is True
    assert [r["id"] for r in out["readings"]] == ["A", "B"]
    assert out["readings"][0]["rejected"] == {}
    assert list(out["readings"][1]["rejected"]) == [
        "kubectl delete namespace platform-ops"
    ]


@pytest.mark.parametrize(
    "model,key",
    [("gemini/gemini-3.1-pro-preview", "GEMINI_API_KEY"), ("anthropic/claude-sonnet-5-5", "ANTHROPIC_API_KEY"),
     ("openai/gpt-5.6", "OPENAI_API_KEY")],
)  # fmt: skip
def test_missing_keys_names_the_provider_variable(monkeypatch, model, key):
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert key in missing_keys(model)
    monkeypatch.setenv(key, "x")
    assert missing_keys(model) == []


def test_parse_response_not_executable():
    out = parse_response(
        '{"executable": false, "note": "blames a service that does not exist", "readings": []}'
    )
    assert out["executable"] is False


class FakeExperiments(engine.ProblemExperiments):
    def __init__(self, rule, repeats=2):
        self.rule, self.calls = rule, []
        self.s = engine.Settings(context="kind-test", out_dir=None, repeats=repeats)

    def recovers(self, commands, kind, rep=1):
        self.calls.append(tuple(commands))
        return self.rule(set(commands))


def test_ddmin_finds_the_one_command_that_matters():
    exp = FakeExperiments(lambda s: "selector" in s)
    assert exp.ddmin(["label-frontend", "selector", "label-search"]) == ["selector"]


def test_ddmin_with_interfering_steps_returns_a_working_subset():
    # The PVC case: {replicas} cures, but {replicas, anti_affinity} does not.
    def rule(s):
        return ("replicas" in s and "anti" not in s) or "pvc" in s

    out = FakeExperiments(rule).ddmin(["replicas", "anti", "pvc"])
    assert rule(set(out)) and len(out) == 1


def test_test_undo_skips_ddmin_when_it_does_not_recover():
    exp = FakeExperiments(lambda s: False)
    res = exp.test_undo(["a", "b"])
    assert res["recovers"] == [False, False] and res["minimal"] is None


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_report_verdicts(tmp_path):
    good = ["kubectl delete networkpolicy deny -n h"]
    bad = ["kubectl rollout restart deployment/rate -n h"]
    _write(tmp_path / "diagnoses.jsonl", [
        {"id": "a/p/1", "problem_id": "p", "agent": "a", "text": "netpol", "judge_score": 100.0,
         "judge_pass": True, "mitigation_pass": True},
        {"id": "b/p/1", "problem_id": "p", "agent": "b", "text": "mongo", "judge_score": 60.0,
         "judge_pass": True, "mitigation_pass": False},
        {"id": "c/p/1", "problem_id": "p", "agent": "c", "text": "two ways", "judge_score": 44.0,
         "judge_pass": False, "mitigation_pass": True},
    ])  # fmt: skip
    _write(tmp_path / "translations.jsonl", [
        {"diagnosis_id": "a/p/1", "problem_id": "p", "executable": True,
         "readings": [{"id": "A", "commands": good, "rejected": {}}]},
        {"diagnosis_id": "b/p/1", "problem_id": "p", "executable": True,
         "readings": [{"id": "A", "commands": bad, "rejected": {}}]},
        {"diagnosis_id": "c/p/1", "problem_id": "p", "executable": True,
         "readings": [{"id": "A", "commands": good, "rejected": {}}, {"id": "B", "commands": bad, "rejected": {}}]},
    ])  # fmt: skip
    trial = {"kind": "undo", "problem": "p"}
    _write(tmp_path / "trials" / "p.jsonl", [
        {**trial, "commands": good, "rep": 1, "verdict": "RECOVERED"},
        {**trial, "commands": good, "rep": 2, "verdict": "RECOVERED"},
        {**trial, "commands": bad, "rep": 1, "verdict": "NOT_RECOVERED"},
        {**trial, "commands": bad, "rep": 2, "verdict": "INVALID"},
        {**trial, "commands": bad, "rep": 2, "verdict": "NOT_RECOVERED"},
    ])  # fmt: skip
    (tmp_path / "problems").mkdir()
    (tmp_path / "problems" / "p.json").write_text(
        json.dumps({"status": "ok", "oracle": "x"})
    )
    report.write(tmp_path)
    rows = {
        r["diagnosis_id"]: r for r in report.diagnosis_rows(report.load(tmp_path), 2)
    }
    assert rows["a/p/1"]["experiment"] == "RECOVERS"
    assert rows["b/p/1"]["experiment"] == "DOES_NOT_RECOVER"
    assert rows["c/p/1"]["experiment"] == "AMBIGUOUS"
    md = (tmp_path / "report.md").read_text()
    assert "| 0 or 100 | 1 | 1 of 1 |" in md and "| between | 1 | 0 of 1 |" in md
    assert "experiment matches 2, the judge matches 1" in md
