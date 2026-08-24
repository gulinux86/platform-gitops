# platform-gitops

Everything that **extends the cluster**, delivered by Argo CD. Applications and
microservices live in a separate repository with different privileges — see
[Boundary](#boundary).

The cluster itself, Argo CD, and the AWS-side plumbing come from
[`eks-terraform`](https://github.com/gulinux86/eks-terraform).

## Two rules

**1. No `kind: Secret` in this repository.** A Secret's `data` is base64, not
encryption — anyone can decode it in one command, and this repository is public.
Real secrets go to AWS Secrets Manager and are referenced with an `ExternalSecret`,
so Git holds the pointer and never the value.

**2. Anything installed here is platform.** If a team owns it and ships it, it
belongs in the application repository.

## Boundary

The split is enforced by Argo CD `AppProject`s, which live in Terraform rather than
here — a permission boundary stored in the repository it governs can be widened by
anyone who can commit to it.

| | `platform` project | `apps` project |
|---|---|---|
| Source | this repository | the application repository |
| Cluster-scoped resources (CRDs, ClusterRole, Namespace) | allowed | **denied** |
| Destination namespaces | any | a fixed list |

So a manifest in the application repository cannot install a CRD, grant itself a
ClusterRoleBinding, or escape its namespace. Argo CD refuses before applying.

## Layout

```
apps/          one Application per component — the "where and in what order"
components/    chart values and manifests    — the "what"
```

Separating them means changing a chart version touches one file and never the
orchestration.

## Sync waves

Argo CD does not infer ordering. A CRD and an instance of it applied together is a
race, and the instance usually loses. The `argocd.argoproj.io/sync-wave`
annotation makes the order explicit:

| Wave | Component | Why here |
|---|---|---|
| 0 | Gateway API CRDs | `Gateway` and `HTTPRoute` are instances of these |
| 1 | cert-manager | Prerequisite for the ADOT operator and for issued TLS |
| 2 | istio-base | Istio's CRDs and cluster roles |
| 3 | istiod | Control plane, `profile: ambient` |
| 4 | istio-cni | Node redirection — ambient has no sidecar to intercept |
| 5 | ztunnel | The per-node proxy that replaces sidecars |
| 6 | example Gateway | Proves the path; delete once real routes exist |

## Why ambient, not sidecars

Ambient moves mTLS and L4 authorization to one `ztunnel` pod per node, instead of
adding a proxy container to every application pod. On a two-node cluster that is
two pods rather than one per workload — and pods, not CPU, are the binding
constraint here: the VPC CNI caps each node by ENI capacity.

The cost is that L7 features (retries, header routing, fine-grained policy) need a
waypoint proxy per namespace, deployed only where that is actually wanted. Sidecar
mode gives L7 everywhere by paying for it everywhere.

## Versions

Every version is pinned. Unpinned, a rebuild installs whatever is newest that day,
so two syncs months apart give different clusters with no diff in Git.

| | |
|---|---|
| Gateway API | `v1.6.1` (standard channel) |
| cert-manager | `v1.21.1` |
| Istio | `1.30.3` |

## Adding a component

1. Values or manifests under `components/<name>/`
2. An `Application` under `apps/`, numbered by its wave
3. Commit — Argo CD reconciles the rest

## Phases

Phase 1 is what is here: traffic path. Then Karpenter, then observability
(kube-prometheus-stack and Kiali), then External Secrets and Argo Rollouts once
there are applications to serve. One phase proven before the next, so a failure has
one plausible cause instead of five.
