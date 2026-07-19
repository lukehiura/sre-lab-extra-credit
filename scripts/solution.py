#!/usr/bin/env python3
"""Option 2 tools: health | conn | fix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import kr8s
from kr8s.objects import Endpoints, Pod, objects_from_files

warnings.filterwarnings("ignore", message="Kubernetes version .* is not supported")

ROOT = Path(__file__).resolve().parents[1]
NS = "sre-lab"
POSTMORTEM_DIR = ROOT / "postmortems"
ALLOWED_ROLLBACKS = frozenset({"nginx", "redis", "redis-client"})
SERVICE_MANIFESTS = {
    "nginx": ROOT / "manifests/nginx.yaml",
    "redis": ROOT / "manifests/redis.yaml",
}


def _alive(pod) -> bool:
    return not pod.raw.get("metadata", {}).get("deletionTimestamp")


def _exec(pod: str, cmd: list[str], namespace: str = NS) -> tuple[int, str]:
    result = Pod.get(pod, namespace=namespace).exec(cmd, check=False, capture_output=True)
    text = ((result.stdout or b"") + (result.stderr or b"")).decode("utf-8", errors="replace")
    return result.returncode, text


def _probe(kind: str, host: str, port: int | None = None, pod: str = "netshoot", namespace: str = NS) -> str:
    if kind == "http":
        rc, text = _exec(pod, ["curl", "-sS", "--max-time", "3", f"http://{host}:{port}/"], namespace)
    elif kind == "tcp":
        rc, text = _exec(pod, ["nc", "-zv", "-w", "3", host, str(port)], namespace)
        if rc == 0 and "succeeded" in text.lower():
            return "ok"
    else:
        return "probe error"
    if rc == 0:
        return "ok"
    lower = text.lower()
    if any(s in lower for s in ("refused", "failed to connect", "could not connect")):
        return "connection refused"
    if "timed out" in lower or "timeout" in lower:
        return "timeout"
    return "probe error"


def _endpoint_count(name: str, namespace: str = NS) -> int:
    try:
        ep = Endpoints.get(name, namespace=namespace)
    except Exception:
        return 0
    return sum(
        1
        for sub in (ep.raw.get("subsets") or [])
        for a in (sub.get("addresses") or [])
        if a.get("ip")
    )


def _ready_pods(selector: dict, namespace: str = NS) -> list:
    if not selector:
        return []
    return [p for p in kr8s.get("pods", namespace=namespace, label_selector=selector) if _alive(p) and p.ready()]


def _owner_deployment(pod) -> str | None:
    for owner in pod.raw.get("metadata", {}).get("ownerReferences") or []:
        if owner.get("kind") != "ReplicaSet":
            continue
        rs = list(kr8s.get("replicasets", namespace=pod.namespace, field_selector=f"metadata.name={owner['name']}"))
        if not rs:
            continue
        for rs_owner in rs[0].raw.get("metadata", {}).get("ownerReferences") or []:
            if rs_owner.get("kind") == "Deployment":
                return rs_owner["name"]
    return None


def health(namespace: str = NS, probe_pod: str = "netshoot") -> int:
    services = list(kr8s.get("services", namespace=namespace))
    if not services:
        print("[]")
        return 1

    results, all_ok = [], True
    for svc in services:
        eps = _endpoint_count(svc.name, namespace)
        ready = len(_ready_pods(dict(svc.spec.selector or {}), namespace))
        port = svc.raw["spec"]["ports"][0]["port"]
        reason, healthy = "ok", True

        if eps == 0:
            reason, healthy = "no endpoints", False
        elif ready == 0:
            reason, healthy = "not ready", False
        else:
            kind = "http" if port == 80 or svc.name == "nginx" else "tcp"
            reason = _probe(kind, svc.name, port, probe_pod, namespace)
            if reason != "ok":
                healthy = False
                if reason == "connection refused":
                    tp = svc.raw["spec"]["ports"][0].get("targetPort")
                    reason = f"connection refused (check targetPort={tp})"

        all_ok = all_ok and healthy
        results.append({
            "service": svc.name,
            "namespace": namespace,
            "healthy": healthy,
            "endpoints": eps,
            "ready_pods": ready,
            "reason": reason,
        })

    print(json.dumps(results))
    return 0 if all_ok else 1


def conn(spec_path: Path, verbose: bool = False) -> int:
    spec = json.loads(spec_path.read_text())
    ns = spec.get("probe_namespace", NS)
    pod = spec.get("probe_pod", "netshoot")
    tests = spec.get("tests", [])
    results, passed, failed = [], 0, 0

    for test in tests:
        host, kind, port, expected = test["to_host"], test["test_type"], test.get("to_port"), test["expect"]

        if kind == "dns":
            rc, out = _exec(pod, ["nslookup", host], ns)
            ok = rc == 0 and "can't find" not in out.lower() and "nxdomain" not in out.lower()
            actual = "pass" if ok else "fail"
            detail = (out.splitlines()[-1] if out and ok else out) or "dns failed"
        elif kind in ("http", "tcp"):
            reason = _probe(kind, host, port, pod, ns)
            actual = "pass" if reason == "ok" else "fail"
            detail = f"{kind} ok" if reason == "ok" else reason
        else:
            actual, detail = "fail", f"unknown test_type {kind}"

        match = actual == expected
        passed += int(match)
        failed += int(not match)
        row = {
            "name": test.get("name", host),
            "to_host": host,
            "to_port": port,
            "test_type": kind,
            "expect": expected,
            "actual": actual,
            "match": match,
            "detail": detail,
        }
        results.append(row)
        if verbose:
            print(f"[{'OK' if match else 'MISMATCH'}] {row['name']}: expect={expected} actual={actual} ({detail})")

    print(json.dumps({
        "probe_namespace": ns,
        "probe_pod": pod,
        "summary": {"passed": passed, "failed": failed, "total": len(tests)},
        "tests": results,
    }))
    return 0 if failed == 0 else 1


def detect() -> list[dict]:
    out: list[dict] = []

    for pol in kr8s.get("networkpolicy", namespace=NS):
        spec = pol.raw.get("spec", {})
        if (
            spec.get("podSelector", {}).get("matchLabels", {}).get("app") == "redis"
            and "Ingress" in (spec.get("policyTypes") or [])
            and not spec.get("ingress")
        ):
            out.append({
                "classification": "networkpolicy_lockout",
                "resource": f"networkpolicy/{pol.name}",
                "evidence": "deny-all ingress on redis",
                "fix": f"delete networkpolicy/{pol.name}",
                "action": {"type": "delete", "kind": "networkpolicy", "name": pol.name},
            })

    for pod in kr8s.get("pods", namespace=NS):
        if not _alive(pod):
            continue
        for cs in pod.raw.get("status", {}).get("containerStatuses") or []:
            reason = (cs.get("state", {}).get("waiting") or {}).get("reason", "")
            if reason not in ("ImagePullBackOff", "ErrImagePull"):
                continue
            deploy = _owner_deployment(pod)
            if deploy in ALLOWED_ROLLBACKS:
                action = {"type": "rollout_undo", "name": deploy}
                fix_text = f"rollout undo deploy/{deploy}"
            else:
                action, fix_text = None, "manual fix required"
            out.append({
                "classification": "bad_rollout",
                "resource": f"pod/{pod.name}",
                "evidence": f"{reason}" + (f", deploy/{deploy}" if deploy else ""),
                "fix": fix_text,
                "action": action,
            })

    for svc in kr8s.get("services", namespace=NS):
        selector = dict(svc.spec.selector or {})
        if selector and _endpoint_count(svc.name) == 0:
            out.append({
                "classification": "missing_endpoints",
                "resource": f"service/{svc.name}",
                "evidence": "empty endpoints with selector set",
                "fix": "check labels vs selector",
                "action": None,
            })
            continue
        if _endpoint_count(svc.name) == 0:
            continue

        pods = _ready_pods(selector)
        if not pods:
            continue
        port_spec = svc.raw["spec"]["ports"][0]
        target = port_spec["targetPort"]
        cports = (pods[0].raw.get("spec", {}).get("containers") or [{}])[0].get("ports") or []
        named = {p.get("name"): p.get("containerPort") for p in cports}
        cport = named.get(target) if isinstance(target, str) else (cports[0].get("containerPort") if cports else None)
        if cport is None or str(target) == str(cport):
            continue
        svc_port = port_spec["port"]
        if (svc_port != 80 and svc.name != "nginx") or _probe("http", svc.name, svc_port) != "connection refused":
            continue
        manifest = SERVICE_MANIFESTS.get(svc.name)
        out.append({
            "classification": "target_port_mismatch",
            "resource": f"service/{svc.name}",
            "evidence": f"targetPort={target}, containerPort={cport}, probe connection refused",
            "fix": f"apply manifests/{svc.name}.yaml" if manifest else "patch targetPort",
            "action": {"type": "apply", "path": str(manifest)} if manifest else None,
        })

    pods = list(kr8s.get("pods", namespace=NS, field_selector="metadata.name=broken-dns-pod"))
    if pods and _alive(pods[0]) and pods[0].raw.get("spec", {}).get("dnsPolicy") == "Default":
        rc, text = _exec("broken-dns-pod", ["nslookup", "nginx"])
        lower = text.lower()
        if rc != 0 or "nxdomain" in lower or "can't find" in lower:
            out.append({
                "classification": "dns_misconfig",
                "resource": "pod/broken-dns-pod",
                "evidence": "dnsPolicy=Default, cluster DNS fails",
                "fix": "delete pod/broken-dns-pod (grace-period=1)",
                "action": {"type": "delete", "kind": "pod", "name": "broken-dns-pod", "grace_period": 1},
            })
    return out


def run_action(action: dict) -> str:
    if action["type"] == "apply":
        done = []
        for obj in objects_from_files(action["path"]):
            if not getattr(obj, "namespace", None):
                obj.namespace = NS
            if obj.exists():
                type(obj).get(obj.name, namespace=obj.namespace).patch(obj.raw)
                done.append(f"patched {obj.kind}/{obj.name}")
            else:
                obj.create()
                done.append(f"created {obj.kind}/{obj.name}")
        return "; ".join(done)

    if action["type"] == "delete":
        objs = list(kr8s.get(action["kind"], namespace=NS, field_selector=f"metadata.name={action['name']}"))
        if not objs:
            return f"{action['kind']}/{action['name']} not found"
        kwargs = {"grace_period": action["grace_period"]} if action.get("grace_period") is not None else {}
        objs[0].delete(**kwargs)
        return f"deleted {action['kind']}/{action['name']}"

    if action["type"] == "rollout_undo":
        name = action["name"]
        undo = subprocess.run(
            ["kubectl", "rollout", "undo", f"deploy/{name}", "-n", NS],
            capture_output=True, text=True,
        )
        if undo.returncode != 0:
            raise RuntimeError((undo.stderr or undo.stdout).strip())
        subprocess.run(
            ["kubectl", "rollout", "status", f"deploy/{name}", "-n", NS, "--timeout=60s"],
            capture_output=True, text=True,
        )
        return (undo.stdout or f"rolled back deploy/{name}").strip()

    raise ValueError(action["type"])


def write_postmortem(report: dict, started: datetime, recovered: bool) -> None:
    if not report.get("remediation"):
        return
    POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)
    ended = datetime.now(timezone.utc)
    path = POSTMORTEM_DIR / f"postmortem-{ended.strftime('%Y%m%dT%H%M%SZ')}.txt"
    lines = [
        f"when: {started.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"status: {'recovered' if recovered else 'still broken'}",
        f"mttr_s: {max(1, int((ended - started).total_seconds()))}",
    ]
    for i, inc in enumerate(report.get("incidents") or []):
        action = (report.get("remediation") or [])[i] if i < len(report.get("remediation") or []) else {}
        lines += [
            f"broke: {inc.get('classification')} on {inc.get('resource')} ({inc.get('evidence')})",
            f"fix: {inc.get('fix')}",
            f"result: {action.get('result') or 'applied'}" if action.get("applied")
            else f"result: skipped ({action.get('note', 'n/a')})",
        ]
    lines.append(f"remaining: {report.get('remaining_incidents', '?')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        report["postmortem"] = str(path.relative_to(ROOT))
    except ValueError:
        report["postmortem"] = str(path)


def _public(report: dict) -> dict:
    out = dict(report)
    out["incidents"] = [{k: v for k, v in i.items() if k != "action"} for i in report.get("incidents") or []]
    return out


def fix(do_fix: bool = False, dry_run: bool = False) -> int:
    if do_fix and dry_run:
        print(json.dumps({"error": "use only one of --fix or --dry-run"}), file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    incidents = detect()
    report = {
        "namespace": NS,
        "incident_count": len(incidents),
        "incidents": incidents,
        "timestamp": started.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    if not incidents or not (do_fix or dry_run):
        print(json.dumps(_public(report)))
        return 0 if not incidents else 1

    remediation = []
    for inc in incidents:
        action = inc.get("action")
        row = {"classification": inc["classification"], "fix": inc["fix"], "applied": False}
        if not action:
            row["note"] = "manual fix required"
        elif dry_run:
            row["note"] = "dry-run"
        else:
            row["result"] = run_action(action)
            row["applied"] = True
        remediation.append(row)
    report["remediation"] = remediation

    if not do_fix:
        print(json.dumps(_public(report)))
        return 1

    remaining: list[dict] = []
    for _ in range(5):
        remaining = detect()
        if not remaining:
            break
        time.sleep(2)
    report["remaining_incidents"] = len(remaining)
    write_postmortem(report, started, not remaining)
    print(json.dumps(_public(report)))
    return 0 if not remaining else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: solution.py {health,conn,fix} ...", file=sys.stderr)
        return 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "health":
        p = argparse.ArgumentParser(prog="solution.py health")
        p.add_argument("namespace", nargs="?", default=NS)
        p.add_argument("probe_pod", nargs="?", default="netshoot")
        a = p.parse_args(rest)
        return health(a.namespace, a.probe_pod)
    if cmd == "conn":
        p = argparse.ArgumentParser(prog="solution.py conn")
        p.add_argument("spec", type=Path)
        p.add_argument("--verbose", action="store_true")
        a = p.parse_args(rest)
        return conn(a.spec, a.verbose)
    if cmd == "fix":
        p = argparse.ArgumentParser(prog="solution.py fix")
        p.add_argument("--fix", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        a = p.parse_args(rest)
        return fix(a.fix, a.dry_run)

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
