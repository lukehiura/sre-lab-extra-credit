#!/bin/bash
set -euo pipefail

wait_for_docker() {
    echo "waiting for docker..."
    local max_attempts=30 attempt=0
    while ! docker info &>/dev/null; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            echo "docker failed to start after $max_attempts attempts" >&2
            exit 1
        fi
        sleep 2
    done
    echo "docker ready"
}

create_cluster() {
    if kind get clusters 2>/dev/null | grep -q "sre-lab"; then
        echo "cluster sre-lab already exists, skipping create"
        return 0
    fi
    echo "creating kind cluster sre-lab"
    kind create cluster --name sre-lab --config kind_config.yaml
}

install_cilium() {
    if kubectl get daemonset cilium -n kube-system &>/dev/null; then
        echo "cilium already installed, waiting for ready"
        cilium status --wait
        return 0
    fi
    echo "installing cilium"
    cilium install \
        --set ipam.mode=kubernetes \
        --set kubeProxyReplacement=false \
        --set socketLB.enabled=false \
        --set externalIPs.enabled=true \
        --set hostPort.enabled=true \
        --set nodePort.enabled=true
    cilium status --wait
}

deploy_application() {
    echo "deploying lab workloads"
    kubectl apply -f manifests/namespace.yaml
    kubectl apply -f manifests/redis.yaml
    kubectl apply -f manifests/redis-client.yaml
    kubectl apply -f manifests/nginx.yaml
    kubectl apply -f manifests/netshoot.yaml
    kubectl -n sre-lab wait --for=condition=ready pod --all --timeout=120s
    echo "workloads ready"
}

wait_for_docker
create_cluster
install_cilium
deploy_application
echo "done. try: kubectl get pods -n sre-lab"
