"""Checks on LLM-written undo commands before they reach a cluster."""

from __future__ import annotations

import re
import shlex

import yaml

ALLOWED_VERBS = {
    "delete",
    "patch",
    "set",
    "scale",
    "label",
    "annotate",
    "apply",
    "create",
    "replace",
    "rollout",
    "exec",
}
ALLOWED_ROLLOUT = {"restart", "undo", "resume"}
PROTECTED_KINDS = {
    "namespace", "namespaces", "ns",
    "node", "nodes", "no",
    "customresourcedefinition", "customresourcedefinitions", "crd", "crds",
    "clusterrole", "clusterroles", "clusterrolebinding", "clusterrolebindings",
    "apiservice", "apiservices",
}  # fmt: skip
PROTECTED_MANIFEST_KINDS = {
    "Namespace",
    "Node",
    "CustomResourceDefinition",
    "ClusterRole",
    "ClusterRoleBinding",
    "APIService",
}
# Deletes are refused here; patches are not (e.g. a CoreDNS ConfigMap).
INFRA_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "local-path-storage",
    "openebs",
    "calico-system",
    "tigera-operator",
    "observe",
    "sregym",
}
_SHELL_CONTROL = re.compile(r"(\|\||&&|[|;`]|\$\(|\$\{|>|<(?!<))")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_FLAGS_WITH_VALUE = {
    "-n",
    "--namespace",
    "-c",
    "--container",
    "-p",
    "--patch",
    "--type",
    "-f",
    "--filename",
    "-l",
}


def split_heredoc(cmd: str) -> tuple[str, str | None]:
    """Return (command line, heredoc body or None)."""
    head, _, rest = cmd.partition("\n")
    m = _HEREDOC.search(head)
    if not m:
        return cmd.strip(), None
    body = rest.rsplit(m.group(1), 1)[0] if m.group(1) in rest else rest
    return head.strip(), body


def _namespace(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a in ("-n", "--namespace") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--namespace="):
            return a.split("=", 1)[1]
        if a.startswith("-n") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def check(cmd: str) -> str | None:
    """Return why the command is refused, or None when it may run."""
    head, body = split_heredoc(cmd)
    if not head.startswith("kubectl "):
        return "not a kubectl command"
    line = _HEREDOC.sub("", head) if body is not None else head
    # The shell does not interpret quoted text, e.g. a JSON patch.
    if _SHELL_CONTROL.search(_QUOTED.sub("''", line)):
        return "shell control characters are not allowed"
    if body is None and "\n" in cmd.strip():
        return "multi-line command without a heredoc"
    try:
        all_args = shlex.split(line)[1:]
    except ValueError as e:
        return f"unparseable quoting: {e}"
    # What follows "--" runs inside the container.
    args = all_args[: all_args.index("--")] if "--" in all_args else all_args
    words, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in _FLAGS_WITH_VALUE:
            skip = True
        elif not a.startswith("-"):
            words.append(a)
    if not words or words[0] not in ALLOWED_VERBS:
        return f"verb {words[0] if words else '(none)'!r} is not allowed"
    if any(a in ("-A", "--all-namespaces") for a in args):
        return "--all-namespaces is not allowed"
    if any(
        a.startswith(("--context", "--kubeconfig", "--server", "--cluster"))
        for a in args
    ):
        return "the command may not choose its own cluster"
    verb = words[0]
    if verb == "rollout" and (len(words) < 2 or words[1] not in ALLOWED_ROLLOUT):
        return "only rollout restart, undo and resume are allowed"
    if verb == "exec":
        ns = _namespace(args)
        if not ns or ns in INFRA_NAMESPACES or "--" not in all_args:
            return "exec needs -n <app namespace> and a command after --"
    if verb == "delete":
        targets = [w.lower() for w in words[1:]]
        kinds = {t.split("/")[0].split(".")[0] for t in targets}
        if kinds & PROTECTED_KINDS:
            return f"deleting {sorted(kinds & PROTECTED_KINDS)} is not allowed"
        if (_namespace(args) or "") in INFRA_NAMESPACES:
            return f"deleting objects in namespace {_namespace(args)} is not allowed"
    if verb in ("apply", "create", "replace") and body is not None:
        try:
            docs = [d for d in yaml.safe_load_all(body) if d]
        except yaml.YAMLError as e:
            return f"heredoc is not valid YAML: {e}"
        bad = {
            d.get("kind") for d in docs if isinstance(d, dict)
        } & PROTECTED_MANIFEST_KINDS
        if bad:
            return f"manifests of kind {sorted(bad)} are not allowed"
    return None


def pin_context(cmd: str, context: str) -> str:
    return cmd.replace("kubectl ", f"kubectl --context {shlex.quote(context)} ", 1)
