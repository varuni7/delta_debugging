"""Translate diagnoses into undo commands. The LLM sees only the diagnosis and the namespace."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from delta_debugging import safety
from delta_debugging.results import Diagnosis

PROMPT_VERSION = 2
DEFAULT_MODEL = "gemini/gemini-3.1-pro-preview"

SYSTEM_PROMPT = """You translate an SRE agent's diagnosis of a Kubernetes incident into the kubectl commands \
that would undo the root cause the diagnosis blames. The commands will be applied to a fresh copy of the \
broken system to test whether the diagnosis is right. Translate faithfully: do not repair the system \
yourself and do not improve on the diagnosis.

Rules:
1. Undo only what the diagnosis blames as the cause. If it names several causes, undo each of them.
2. If the diagnosis states a remedy (for example "roll out the gateway") instead of a changed object to \
revert, translate that remedy.
3. Do not add restarts, scaling, resource increases or other hardening unless the diagnosis presents \
them as the fix.
4. Restore values the diagnosis says were correct. If it implies the correct value without stating it, \
you may infer it, and set "inferred": true on that reading.
5. Refer to stable objects (Deployments, Services, ConfigMaps, Secrets, NetworkPolicies, webhook \
configurations, and so on), never to pod or ReplicaSet names.
6. Each command is one kubectl invocation, with -n <namespace> for namespaced objects. No pipes, no \
"&&", no ";", no shell variables, and no kubectl edit, cp, port-forward, run or debug. Use kubectl exec \
(-n <namespace> deploy/<name> -- <command>) only when the cause lives inside a running process or \
database and cannot be undone through the Kubernetes API, for example a revoked database permission. To \
supply a manifest, use: kubectl apply -f - <<'EOF' followed by the YAML and a final line EOF.
7. If the text supports two or more different actions (for example, deleting a webhook configuration \
versus deleting the controller that serves it), give each as a separate reading. Do not create readings \
that differ only in syntax. At most 3 readings.
8. If the diagnosis blames something that cannot be undone with kubectl (a component that does not \
exist, a cause outside the cluster) or names no cause at all, set "executable": false and say why in \
"note".

Respond with JSON only, in this shape:
{"executable": true, "note": "", "readings": [{"summary": "<one sentence: what this reading undoes>", \
"inferred": false, "commands": ["kubectl ..."]}]}"""


def user_prompt(diagnosis: str, namespace: str | None) -> str:
    ns = namespace or "unknown (use the namespace named in the diagnosis)"
    return f"Application namespace: {ns}\n\nDiagnosis:\n<<<\n{diagnosis}\n>>>"


def parse_response(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    data = json.loads(text[start : end + 1])
    readings = []
    for i, r in enumerate(data.get("readings") or []):
        cmds = [
            c.strip()
            for c in r.get("commands") or []
            if isinstance(c, str) and c.strip()
        ]
        rejected = {c: why for c in cmds if (why := safety.check(c))}
        readings.append(
            {
                "id": "ABC"[i] if i < 3 else str(i),
                "summary": r.get("summary", ""),
                "inferred": bool(r.get("inferred")),
                "commands": cmds,
                "rejected": rejected,
            }
        )
    executable = bool(data.get("executable", True)) and any(
        r["commands"] for r in readings
    )
    return {
        "executable": executable,
        "note": data.get("note", ""),
        "readings": readings[:3],
    }


def namespaces_for(problem_ids: set[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    try:
        from sregym.conductor.problems.registry import ProblemRegistry

        registry = ProblemRegistry()
    except Exception:
        return dict.fromkeys(problem_ids)
    for pid in problem_ids:
        try:
            out[pid] = registry.get_problem_instance(pid).namespace
        except Exception:
            out[pid] = None
    return out


def missing_keys(model: str) -> list[str]:
    import litellm

    env = litellm.validate_environment(model=model)
    return [] if env.get("keys_in_environment") else env.get("missing_keys", [])


def translate(
    diagnoses: list[Diagnosis],
    model: str,
    out_path: Path,
    *,
    api_base: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    done = {}
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            rec = json.loads(line)
            done[rec["diagnosis_id"]] = rec
    todo = [d for d in diagnoses if d.id not in done]
    namespaces = namespaces_for({d.problem_id for d in todo})
    if dry_run:
        for d in todo:
            print(
                f"--- {d.id} (namespace {namespaces[d.problem_id]})\n{user_prompt(d.text, namespaces[d.problem_id])}\n"
            )
        print(
            f"{len(todo)} diagnoses would be sent to {model}; {len(done)} already translated."
        )
        return list(done.values())

    import litellm

    litellm.drop_params = (
        True  # e.g. temperature on models that only accept the default
    )
    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for n, d in enumerate(todo, 1):
        rec = {
            "diagnosis_id": d.id,
            "problem_id": d.problem_id,
            "agent": d.agent,
            "namespace": namespaces[d.problem_id],
            "model": model,
            "prompt_version": PROMPT_VERSION,
        }
        if not d.text:
            rec.update(executable=False, note="no diagnosis was submitted", readings=[])
        else:
            prompt = user_prompt(d.text, namespaces[d.problem_id])
            rec["prompt"] = prompt
            try:
                resp = litellm.completion(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    **({"api_base": api_base} if api_base else {}),
                )
                rec["raw"] = resp.choices[0].message.content or ""
                rec.update(parse_response(rec["raw"]))
            except Exception as e:  # not saved, so the next run retries it
                rec.update(
                    executable=False,
                    note=f"translation failed: {type(e).__name__}: {e}",
                    readings=[],
                    error=True,
                )
        if not rec.get("error"):
            with out_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        done[d.id] = rec
        status = "error" if rec.get("error") else f"{len(rec['readings'])} reading(s)"
        print(f"[{n}/{len(todo)}] {d.id}: {status}")
    return list(done.values())
