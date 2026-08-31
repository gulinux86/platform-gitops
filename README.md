# platform-gitops

Everything that **extends the cluster**, delivered by Argo CD. Applications and
microservices live in a separate repository with different privileges — see
[Boundary](#boundary).

The cluster itself, Argo CD, and the AWS-side plumbing come from
[`eks-terraform`](https://github.com/gulinux86/eks-terraform).

## Three rules

**1. No `kind: Secret` in this repository.** A Secret's `data` is base64, not
encryption — anyone can decode it in one command, and this repository is public.
Real secrets go to AWS Secrets Manager and are referenced with an `ExternalSecret`,
so Git holds the pointer and never the value.

**2. Nothing here creates an AWS resource.** No `type: LoadBalancer`, no
`kind: Ingress`. A resource a controller creates from a manifest never enters
Terraform state, so nothing can destroy it in order — see
[Where the traffic comes from](#where-the-traffic-comes-from) for the four failed
teardowns that established this.

**3. Anything installed here is platform.** If a team owns it and ships it, it
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
| 6 | example Gateway | Proves the path end to end; delete once real routes exist |

## Where the traffic comes from

Nothing in this repository creates an AWS resource, and that is a rule rather than
a coincidence.

The `Gateway` in `components/examples/` sets its generated Service to `ClusterIP`.
Istio's default is `LoadBalancer`, and the AWS Load Balancer Controller answers a
LoadBalancer Service by creating a network load balancer — one that Terraform never
records, whose interfaces then hold the private subnets, and whose subnets hold the
VPC. Four consecutive `terraform destroy` runs failed exactly that way before this
was changed.

The load balancer now lives in
[`eks-terraform`](https://github.com/gulinux86/eks-terraform)
(`workload/modules/platform-ingress`) and reaches these pods through a
`TargetGroupBinding` that Terraform applies. So the edge is still an AWS load
balancer; it simply belongs to the layer that can also destroy it.

```
here (Argo CD)                    eks-terraform (Terraform)
──────────────                    ─────────────────────────
Gateway                           aws_lb  (internal ALB)
   │ istiod                       aws_lb_target_group
   ▼                              aws_lb_listener
Service: ClusterIP  ◀──registers──  TargetGroupBinding
Deployment (gateway pods)
```

The `ClusterIP` is forced through `spec.infrastructure.parametersRef` pointing at a
ConfigMap with a strategic merge patch — Istio's documented customisation path for
Gateway API. The `networking.istio.io/service-type` annotation is widely cited and
would be shorter, but it does not appear in Istio's annotation reference, and a
teardown guarantee should not rest on undocumented behaviour.

This is what rule 2 is protecting. The other half of it: **a new edge is a
Terraform change**, not a manifest. For one shared gateway that is the intended
trade; it would be the wrong one for a platform giving each team its own edge.

The ConfigMap must live in the `Gateway`'s namespace. Istio ignores a
`parametersRef` pointing outside it *silently*, and the Service reverts to
`LoadBalancer`.

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

Phase 1 is what is here, and it is **proven**: traffic reaches the demo backend
through the ALB, the gateway and the ambient mesh, and the platform tears down
cleanly with all of it running — which it did not, for a while.

Next is Karpenter, then observability (kube-prometheus-stack and Kiali), then
External Secrets and Argo Rollouts once there are applications to serve. One phase
proven before the next, so a failure has one plausible cause instead of five.

Karpenter is where rule 2 gets its next test. Its nodes are EC2 instances outside
Terraform state and their network interfaces hold subnets — the same shape of
problem the ingress path had, arriving from a different direction.
