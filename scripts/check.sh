#!/bin/bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then PY=python3; fi

pass=0
fail=0

check() {
    local desc="$1"
    shift
    if "$@" &>/dev/null; then
        echo "ok  $desc"
        pass=$((pass + 1))
    else
        echo "fail $desc"
        fail=$((fail + 1))
    fi
}

check "kind cluster sre-lab" bash -c 'kind get clusters 2>/dev/null | grep -qx sre-lab'
check "kubectl api" kubectl cluster-info
check "nodes ready" bash -c '
  out=$(kubectl get nodes --no-headers 2>/dev/null) || exit 1
  [ -n "$out" ] || exit 1
  ! echo "$out" | grep -q NotReady
'
check "cilium" bash -c 'kubectl get ds -n kube-system cilium -o jsonpath="{.status.numberReady}" | grep -qE "^[1-9]"'
check "ns sre-lab" kubectl get namespace sre-lab
check "nginx running" bash -c 'kubectl get pods -n sre-lab -l app=nginx --no-headers | grep -q Running'
check "redis running" bash -c 'kubectl get pods -n sre-lab -l app=redis --no-headers | grep -q Running'
check "redis-client running" bash -c 'kubectl get pods -n sre-lab -l app=redis-client --no-headers | grep -q Running'
check "netshoot running" bash -c 'kubectl get pods -n sre-lab -l app=netshoot --no-headers | grep -q Running'

np_count=$(kubectl get networkpolicy -n sre-lab --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$np_count" -eq 0 ]; then
    echo "ok  no networkpolicies"
    pass=$((pass + 1))
else
    echo "warn $np_count networkpolicies present"
fi

if "$PY" "$ROOT/scripts/solution.py" health sre-lab >/dev/null 2>&1; then
    echo "ok  service health"
    pass=$((pass + 1))
else
    echo "fail service health"
    fail=$((fail + 1))
fi

if "$PY" "$ROOT/scripts/solution.py" conn "$ROOT/specs/sre-lab-open.json" >/dev/null 2>&1; then
    echo "ok  conn matrix"
    pass=$((pass + 1))
else
    echo "fail conn matrix"
    fail=$((fail + 1))
fi

echo "$pass ok / $fail fail"
exit $fail
