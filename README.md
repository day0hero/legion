# Legion - Multi-Tenant Cluster Provisioning Pattern

A [Validated Pattern](https://validatedpatterns.io/) for multi-tenant OpenShift cluster provisioning via Tekton pipelines, supporting both **Hive** (full OpenShift) and **HyperShift** (hosted control planes) on AWS.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Hub Cluster                                                     │
│                                                                 │
│  ┌─────────────────────────────────────┐                        │
│  │ Operators (deployed by pattern)     │                        │
│  │  - ACM / MCE / Hive / HyperShift   │                        │
│  │  - OpenShift Pipelines (Tekton)     │                        │
│  │  - Vault + External Secrets         │                        │
│  └─────────────────────────────────────┘                        │
│                                                                 │
│  ┌─────────────────────────────────────┐                        │
│  │ cluster-provisioning namespace      │                        │
│  │  - Pipeline: deploy-hive-cluster    │                        │
│  │  - Pipeline: deploy-hypershift      │                        │
│  │  - Pipeline: destroy-hive-cluster   │                        │
│  │  - Pipeline: destroy-hypershift     │                        │
│  │  - ServiceAccount + RBAC            │                        │
│  │  - ESO ExternalSecrets (creds)      │                        │
│  └─────────────────────────────────────┘                        │
│                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ Per-Cluster NS    │  │ Per-Cluster NS    │                    │
│  │ - ClusterDeploy   │  │ - HostedCluster   │                   │
│  │ - MachinePool     │  │ - NodePool        │                   │
│  │ - Kubeconfig      │  │ - Kubeconfig      │                   │
│  └──────────────────┘  └──────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- OpenShift 4.14+
- `oc` CLI
- `podman` (for pattern installation)
- AWS credentials with permissions for EC2, Route53, IAM, S3
- Red Hat pull secret
- For HyperShift: IAM role ARN configured for STS

## Installation

1. Fork and clone this repository
2. Copy and populate secrets:
   ```bash
   cp values-secret.yaml.template ~/values-secret-legion.yaml
   # Edit with your credentials
   ```
3. Install the pattern:
   ```bash
   ./pattern.sh make install
   ```

## Usage

### Provisioning a Hive Cluster (full OpenShift)

```bash
oc create -f examples/pipelineruns/pipelinerun-hive-aws.yaml
```

Or edit the parameters inline:

```bash
oc create -n cluster-provisioning -f - <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: deploy-hive-
spec:
  pipelineRef:
    name: deploy-hive-cluster
  params:
  - name: cluster-name
    value: my-cluster
  - name: base-domain
    value: example.com
  - name: cloud-provider
    value: aws
  - name: cloud-region
    value: us-east-2
  - name: control-plane-machine-type
    value: m5.xlarge
  - name: worker-machine-type
    value: m5.xlarge
  taskRunTemplate:
    serviceAccountName: legion-provisioner
  workspaces:
  - name: install-config
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 1Gi
  - name: kubeconfig
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 1Gi
EOF
```

### Provisioning a HyperShift Cluster (hosted control plane)

```bash
oc create -f examples/pipelineruns/pipelinerun-hypershift-aws.yaml
```

### Destroying a Cluster

```bash
# Hive
oc create -f examples/pipelineruns/pipelinerun-destroy-hive.yaml

# HyperShift
oc create -f examples/pipelineruns/pipelinerun-destroy-hypershift.yaml
```

### Monitoring

```bash
# Watch pipeline runs
tkn pipelinerun list -n cluster-provisioning

# Follow a specific run
tkn pipelinerun logs <name> -n cluster-provisioning -f

# Check cluster status (Hive)
oc get clusterdeployment -A

# Check cluster status (HyperShift)
oc get hostedcluster -A
```

## Workstreams

### Engineer Self-Service
Engineers submit `PipelineRun` resources (directly or via a git-push trigger) specifying cluster type and parameters. The pipeline handles namespace creation, credential injection, provisioning, and ACM registration.

### QE CI/CD
QE teams integrate cluster provisioning into their CI pipelines by creating `PipelineRun` resources. Clusters can be provisioned on-demand and destroyed after test suites complete.

## Chart Structure

```
charts/
└── hub/
    ├── legion-credentials/     # ESO ExternalSecrets for cloud creds
    │   └── templates/
    │       ├── eso-aws-credentials.yaml
    │       ├── eso-pullsecret.yaml
    │       ├── eso-hypershift-iam.yaml
    │       └── eso-push-secret.yaml
    └── legion-pipelines/       # Tekton pipelines, tasks, RBAC
        └── templates/
            ├── serviceaccount.yaml
            ├── pipeline-deploy-hive.yaml
            ├── pipeline-deploy-hypershift.yaml
            ├── pipeline-destroy-hive.yaml
            ├── pipeline-destroy-hypershift.yaml
            ├── task-create-namespace.yaml
            ├── task-create-externalsecrets.yaml
            ├── task-create-managed-cluster.yaml
            ├── task-create-import-secret.yaml
            ├── task-create-klusterlet-addon.yaml
            ├── task-hive-create-install-config.yaml
            ├── task-hive-create-cluster-deployment.yaml
            ├── task-hive-create-machinepool.yaml
            ├── task-hive-wait-cluster-ready.yaml
            ├── task-hypershift-create-cluster.yaml
            └── task-hypershift-wait-ready.yaml
```

## Phase 2 (Future)

- Tekton Triggers / EventListener for git-push driven provisioning
- Multi-cluster fan-out (request N clusters in one manifest)
- TTL-based cluster reaper (CronJob)
- Azure / GCP support
- Custom CRD/controller for unified provisioning abstraction
