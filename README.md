# SRE Lab Extra Credit: Automating Operations

This is my extra credit for the CS 6250 Kubernetes SRE tutorial (Option 2). The tutorial has you break Services, NetworkPolicies, rollouts, and DNS and then debug them with kubectl and netshoot. I automated that loop on the same sre-lab kind cluster: detect the problem, classify it, dry-run or apply a fix, and write a short postmortem.

Most of the code is in `scripts/solution.py`:

- `health` checks if Services are reachable
- `conn` runs the connectivity matrix
- `fix` finds the fault and can preview (`--dry-run`) or apply (`--fix`) an allow-listed fix

Successful `--fix` runs drop a postmortem file in `postmortems/`.

## What you need

- Docker Desktop
- kubectl
- kind
- cilium-cli
- Python 3.10+

I tested this on macOS. On Windows, WSL2 + Docker Desktop should work.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./scripts/setup.sh
./scripts/check.sh
```

You should see `12 ok / 0 fail` from check.sh. If you need to start over:

```bash
kind delete cluster --name sre-lab
```

Then run setup again.

## How to run the demo

This uses incident-01 (broken nginx Service). The other Part 5 faults work the same way.

Baseline:

```bash
./scripts/check.sh
python scripts/solution.py health sre-lab
python scripts/solution.py conn specs/sre-lab-open.json
```

Break it:

```bash
kubectl apply -f manifests/incidents/incident-01-nginx-service-broken.yaml
```

Detect / preview / fix:

```bash
python scripts/solution.py health sre-lab
python scripts/solution.py fix
python scripts/solution.py fix --dry-run
python scripts/solution.py fix --fix
```

Make sure it recovered:

```bash
python scripts/solution.py health sre-lab
./scripts/check.sh
ls postmortems/
```

Other faults if you want to try them:

```bash
kubectl apply -f manifests/incidents/incident-02-redis-networkpolicy-deny.yaml
kubectl apply -f manifests/incidents/incident-03-nginx-bad-image.yaml
kubectl apply -f manifests/incidents/incident-04-dns-wrong-config.yaml
```
