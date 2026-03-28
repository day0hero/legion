#!/usr/bin/env python3
"""
Generate LegionCluster claims from a pattern's pattern-metadata.yaml.

Reads the cluster sizing requirements defined in a validated pattern's
metadata file and produces ready-to-apply LegionCluster claim manifests
for both Crossplane and Tekton PipelineRun workflows.

Usage:
    # Crossplane claims (default)
    ./generate-claims.py /path/to/pattern-metadata.yaml \
        --cloud aws --base-domain qe.example.com

    # Tekton PipelineRun manifests
    ./generate-claims.py /path/to/pattern-metadata.yaml \
        --cloud aws --base-domain qe.example.com --output-format pipelinerun

    # Only the hub cluster
    ./generate-claims.py /path/to/pattern-metadata.yaml \
        --cloud aws --base-domain qe.example.com --roles hub

    # HyperShift hosted control planes
    ./generate-claims.py /path/to/pattern-metadata.yaml \
        --cloud aws --base-domain qe.example.com --cluster-type hcp \
        --aws-account-id 123456789012
"""

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


CLOUD_COMPOSITION_MAP = {
    "aws": {"hive": "hive-aws", "hcp": "hcp-aws"},
}

DEFAULT_CREDENTIALS = {
    "secretStoreName": "vault-backend",
    "awsCredsVaultKey": "secret/data/hub/aws",
    "pullSecretVaultKey": "pushsecrets/global-pull-secret",
}

DEFAULT_HCP_SETTINGS = {
    "oidcBucketName": "legion-hypershift-oidc",
    "oidcBucketRegion": "us-east-2",
    "endpointAccess": "Public",
}


def load_metadata(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


def extract_sizing(metadata: dict, role: str, cloud: str) -> dict | None:
    """Extract compute and control-plane sizing for a given role and cloud."""
    reqs = metadata.get("requirements", {}).get(role)
    if not reqs:
        return None

    compute = reqs.get("compute", {}).get("platform", {}).get(cloud)
    control_plane = reqs.get("controlPlane", {}).get("platform", {}).get(cloud)

    if not compute or not control_plane:
        return None

    if compute.get("replicas", 0) == 0 and control_plane.get("replicas", 0) == 0:
        return None

    # Spoke with 0 compute workers but >0 control plane replicas is valid (SNO/compact)
    # but only for Hive; for HCP the control plane is hosted, so 0 workers = nothing to do
    

    return {
        "instanceType": compute["type"],
        "workerReplicas": compute["replicas"],
        "controlPlaneMachineType": control_plane["type"],
        "controlPlaneReplicas": control_plane["replicas"],
    }


def build_crossplane_claim(
    pattern_name: str,
    role: str,
    sizing: dict,
    cluster_type: str,
    cloud: str,
    base_domain: str,
    region: str,
    name_prefix: str,
    namespace: str,
    aws_account_id: str | None,
    cluster_version: str | None,
) -> dict:
    cluster_name = f"{name_prefix}-{pattern_name}-{role}"
    composition = CLOUD_COMPOSITION_MAP.get(cloud, {}).get(cluster_type)
    if not composition:
        print(f"WARNING: No composition for cloud={cloud} type={cluster_type}", file=sys.stderr)
        composition = f"{cluster_type}-{cloud}"

    claim = {
        "apiVersion": "legion.io/v1alpha1",
        "kind": "LegionCluster",
        "metadata": {
            "name": cluster_name,
            "namespace": namespace,
        },
        "spec": {
            "compositionRef": {"name": composition},
            "clusterType": cluster_type,
            "clusterName": cluster_name,
            "baseDomain": base_domain,
            "cloudRegion": region,
            "instanceType": sizing["instanceType"],
            "workerReplicas": sizing["workerReplicas"],
            "credentials": dict(DEFAULT_CREDENTIALS),
        },
    }

    if cluster_version:
        claim["spec"]["clusterVersion"] = cluster_version

    if cluster_type == "hive":
        claim["spec"]["controlPlaneReplicas"] = sizing["controlPlaneReplicas"]
        claim["spec"]["controlPlaneMachineType"] = sizing["controlPlaneMachineType"]
    elif cluster_type == "hcp":
        hcp_conf = dict(DEFAULT_HCP_SETTINGS)
        hcp_conf["oidcBucketRegion"] = region
        if aws_account_id:
            hcp_conf["awsAccountId"] = aws_account_id
        claim["spec"]["hypershift"] = hcp_conf

    return claim


def build_pipelinerun(
    pattern_name: str,
    role: str,
    sizing: dict,
    cluster_type: str,
    cloud: str,
    base_domain: str,
    region: str,
    name_prefix: str,
    namespace: str,
    cluster_version: str | None,
) -> dict:
    cluster_name = f"{name_prefix}-{pattern_name}-{role}"
    pipeline_name = "deploy-hypershift-cluster" if cluster_type == "hcp" else "deploy-hive-cluster"

    params = [
        {"name": "cluster-name", "value": cluster_name},
        {"name": "base-domain", "value": base_domain},
        {"name": "cloud-region", "value": region},
    ]

    if cluster_type == "hive":
        params.extend([
            {"name": "cloud-provider", "value": cloud},
            {"name": "control-plane-replicas", "value": str(sizing["controlPlaneReplicas"])},
            {"name": "control-plane-machine-type", "value": sizing["controlPlaneMachineType"]},
            {"name": "worker-replicas", "value": str(sizing["workerReplicas"])},
            {"name": "worker-machine-type", "value": sizing["instanceType"]},
        ])
        if cluster_version:
            params.append({"name": "cluster-version", "value": cluster_version})
    else:
        params.extend([
            {"name": "node-pool-replicas", "value": str(sizing["workerReplicas"])},
            {"name": "instance-type", "value": sizing["instanceType"]},
        ])

    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "generateName": f"{cluster_name}-",
            "namespace": namespace,
        },
        "spec": {
            "pipelineRef": {"name": pipeline_name},
            "taskRunTemplate": {
                "serviceAccountName": "legion-provisioner",
            },
            "workspaces": [
                {"name": "install-config", "emptyDir": {}},
                {"name": "kubeconfig", "emptyDir": {}},
            ],
            "params": params,
        },
    }


def determine_cluster_type(metadata: dict, requested_type: str) -> str:
    """Respect the requested type but warn if the pattern doesn't support it."""
    features = metadata.get("extra_features", {})
    if requested_type == "hcp" and not features.get("hypershift_support", False):
        print(
            f"WARNING: Pattern does not declare hypershift_support, "
            f"proceeding with hcp anyway",
            file=sys.stderr,
        )
    return requested_type


def main():
    parser = argparse.ArgumentParser(
        description="Generate LegionCluster claims from pattern-metadata.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("metadata_file", help="Path to pattern-metadata.yaml")
    parser.add_argument("--cloud", default="aws", choices=["aws", "gcp", "azure"])
    parser.add_argument("--cluster-type", default="hive", choices=["hive", "hcp"])
    parser.add_argument("--base-domain", required=True, help="Base DNS domain")
    parser.add_argument("--region", default="us-east-2", help="Cloud region")
    parser.add_argument("--name-prefix", default="qe", help="Prefix for cluster names")
    parser.add_argument("--namespace", default="cluster-provisioning")
    parser.add_argument("--cluster-version", default=None, help="OCP version or image set")
    parser.add_argument(
        "--roles", nargs="+", default=["hub", "spoke"],
        help="Which cluster roles to generate (default: hub spoke)",
    )
    parser.add_argument(
        "--output-format", default="crossplane",
        choices=["crossplane", "pipelinerun"],
        help="Output Crossplane claims or Tekton PipelineRun manifests",
    )
    parser.add_argument(
        "--aws-account-id", default=None,
        help="AWS account ID (required for HCP cluster type)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print summary instead of YAML")

    args = parser.parse_args()
    metadata = load_metadata(args.metadata_file)
    pattern_name = metadata.get("name", "unknown")
    cluster_type = determine_cluster_type(metadata, args.cluster_type)

    if cluster_type == "hcp" and args.output_format == "crossplane" and not args.aws_account_id:
        print("ERROR: --aws-account-id is required for HCP Crossplane claims", file=sys.stderr)
        sys.exit(1)

    docs = []
    for role in args.roles:
        sizing = extract_sizing(metadata, role, args.cloud)
        if not sizing:
            print(f"INFO: Skipping {role} (no sizing or zero replicas for {args.cloud})", file=sys.stderr)
            continue

        if args.dry_run:
            cluster_name = f"{args.name_prefix}-{pattern_name}-{role}"
            print(f"  {role}: {cluster_name}")
            print(f"    type:           {cluster_type}")
            print(f"    instanceType:   {sizing['instanceType']}")
            print(f"    workers:        {sizing['workerReplicas']}")
            print(f"    controlPlane:   {sizing['controlPlaneMachineType']} x{sizing['controlPlaneReplicas']}")
            continue

        if args.output_format == "crossplane":
            doc = build_crossplane_claim(
                pattern_name, role, sizing, cluster_type, args.cloud,
                args.base_domain, args.region, args.name_prefix, args.namespace,
                args.aws_account_id, args.cluster_version,
            )
        else:
            doc = build_pipelinerun(
                pattern_name, role, sizing, cluster_type, args.cloud,
                args.base_domain, args.region, args.name_prefix, args.namespace,
                args.cluster_version,
            )
        docs.append(doc)

    if not args.dry_run:
        if not docs:
            print("WARNING: No claims generated. Check roles and cloud provider.", file=sys.stderr)
            sys.exit(1)
        print(yaml.dump_all(docs, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
