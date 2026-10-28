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
| Destination namespaces | any | the `app-*` prefix |

So a manifest in the application repository cannot install a CRD, grant itself a
ClusterRoleBinding, or escape its namespace. Argo CD refuses before applying.

## Layout

```
apps/          one Application per component — the "where and in what order"
components/    chart values and manifests    — the "what"
tenants/       one file per application: its Namespace and its Application
```

Separating them means changing a chart version touches one file and never the
orchestration.

`tenants/` is how an application from
[`app-gitops`](https://github.com/gulinux86/app-gitops) is admitted. Each file holds
what the application repository may not decide for itself: the Namespace, its mesh
and gateway labels, and an Application pinned to the `apps` project. The Application
objects live here rather than there because an Application names its own project —
a repository that can write them could name `platform`.

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
| 7 | Karpenter | Provisions nodes for pods that do not fit |
| 8 | EC2NodeClass + NodePool | Instances of wave 7's CRDs, so they follow it |
| 9 | kube-prometheus-stack | The platform starts reporting on itself |
| 10 | Kiali | Reads Prometheus, so it follows it |
| 11 | `tenants` | Application onboarding from `tenants/`; their pods need wave 8's NodePool and wave 6's gateway |

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

## Availability through a node roll

Every node is replaced in an EKS upgrade, and every request crosses the same few
components on the way in: the ALB, the gateway pods, istiod (which issues the
certificate a new pod's ztunnel needs before it can connect) and CoreDNS. An
application with three replicas and a PDB is only as available as the least
protected of those. So each runs **two replicas, spread across nodes, under a PDB
allowing one out**:

| Component | Where it is set |
|---|---|
| Gateway | `components/examples/gateway.yaml` — `deployment` and `podDisruptionBudget` patches in the ConfigMap |
| istiod | `components/istio/istiod.yaml` — `autoscaleMin: 2`; the chart renders the PDB only above one |
| Karpenter | `components/karpenter/values.yaml` — `replicas: 2`; the chart adds the required anti-affinity and the PDB |
| CoreDNS, AWS Load Balancer Controller | `eks-terraform` — EKS add-on configuration and the controller's chart values |

The gateway also has two things that are about the ALB rather than about Kubernetes:
a readiness gate (the namespace's `elbv2.k8s.aws/pod-readiness-gate-inject` label),
so a new pod is not Ready until the ALB sees its target healthy; and a 15-second
`preStop` sleep, so a terminating pod keeps serving until the ALB has deregistered
it.

`eks-upgrade` in `eks-terraform` refuses to start while any of these falls short —
fewer than two Ready replicas, all on one node, no PDB allowing a disruption, or a
gateway pod without its readiness gate. Two of those are what a *fresh* cluster's
bootstrap order leaves behind, not a wrong setting here: replicas placed while only one
node had registered (a topology spread never moves a pod once placed), and gateway
pods created before the TargetGroupBinding that injects their gate. The upgrade
restarts just those Deployments before it begins. It then probes this path through
the ALB for the whole run: the 1.35 → 1.36 rehearsal on hml served 6,826 requests
through it with none failed.

Change any of the settings above and the next upgrade will say so before it touches
the cluster — the list it checks is `CRITICAL` in `scripts/eks-upgrade.sh`.

## Capacity

Nodes arrive when pods need them. A `NodePool` says what may be provisioned and how
much; an `EC2NodeClass` says what a node is made of. Both reference AWS by name or
by tag — never by ARN — for the same reason the controller uses Pod Identity: this
repository is public.

Four things about that pair are worth knowing before changing them.

**The instance list is explicit, and that is not the general advice.** AWS
recommends breadth: the more types Karpenter may use, the better EC2 optimises, and
the wider the pool Spot draws from. This account is on the **AWS Free Plan**, where
anything not free-tier eligible is refused outright — expressed as categories, the
whole `CreateFleet` fails with `InvalidParameterCombination`. Two types survive that
filter with enough memory to be useful. Delete the list and restore categories the
day the account leaves the Free Plan.

**`karpenter.sh/` is a reserved tag prefix.** Keys under it are dropped from
`spec.tags` silently — no error, no warning, no event, while a custom tag beside
them applies normally. The selectors above use `karpenter.sh/discovery` and are
correct: those match a tag Terraform put on the *subnets and security groups*, which
is a different thing from tagging an instance.

**Consolidation constrains workloads, not just nodes.** Karpenter packs by
*requests*, so a pod that bursts above its memory request can land on a node that
cannot hold it. Set `requests` equal to `limits` for non-CPU resources on anything
that runs here.

**A drain has a deadline.** Expiry (`expireAfter: 24h`) is forceful in Karpenter v1:
the node is cordoned whatever its pods say, and without `terminationGracePeriod` a pod
whose PDB never allows an eviction would keep it draining for good — drift too, which
would hang eks-upgrade's last phase. After 30 minutes the remaining pods are deleted,
PDB or not. A workload that cannot afford that needs two replicas, not a stricter PDB.

## Observability

Prometheus, Grafana and Kiali, in the cluster rather than in AWS. The reason is Kiali:
it reads Istio telemetry from Prometheus, and the mesh is the specific thing that was
invisible — ambient has no sidecar to inspect, so until this existed the mesh was
evidenced only by requests succeeding.

**Three things the chart ships are deliberately not installed.** Alertmanager has no
destination here — no Slack, no PagerDuty, no on-call — and unconfigured it would make
alerting look present when a failure would reach nobody. The etcd, scheduler,
controller-manager and kube-proxy rules go with it, along with their scrape targets:
EKS manages those and does not expose them, so they would be permanently-down targets
firing alerts nobody can act on.

**Retention is hours and there is no volume.** This environment is destroyed between
sessions, so metrics do not survive a teardown. That is also most of what makes
Prometheus light enough to fit beside everything else on two nodes.

### L7 metrics come from the gateway

This is the ambient detail that matters for anything added here:

```
ztunnel   L4   istio_tcp_* — connections, bytes.  No status codes.
gateway   L7   istio_requests_total — what Kiali draws its graph from
waypoint  L7   the same, for pod-to-pod traffic — not deployed
```

With only istiod and ztunnel scraped, `istio_requests_total` did not exist and Kiali
had nothing to draw. The gateway pod is scraped on `:15020` for that reason.

Pod-to-pod request detail needs a waypoint per namespace — the L7 cost described
below. Without one, Kiali shows TCP connections between workloads rather than
requests.

### Kiali needs traffic

Its graph comes from counters that only move while requests flow. A quiet cluster
renders an empty topology, which looks like a mesh problem and is not one. Generate
traffic through the gateway before opening it.

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
| Karpenter | `1.13.0` (OCI chart) |
| kube-prometheus-stack | `90.0.0`, with Grafana pinned to `12.1.1` |
| Kiali | `2.31.0` |

Grafana is pinned **off** the chart's default, which is the opposite problem. Chart
90.0.0 points at `grafana:13.2.1-distroless`, and that variant ships without the core
datasource plugins — the provisioned Prometheus datasource then has the right URL, a
reachable Service, and no driver behind it, so every query silently returns nothing.
Unpin when the chart moves to a 13.x image that is not distroless.

The one exception to pinning is the node AMI: `al2023@latest` in the `EC2NodeClass`. Patches
then arrive unattended, at the cost of two syncs months apart producing different
nodes with no diff here — the property this table exists to prevent, accepted
because hml is rebuilt from scratch and has no patching pipeline. Production should
pin the alias.

## Adding a component

1. Values or manifests under `components/<name>/`
2. An `Application` under `apps/`, numbered by its wave
3. Commit — Argo CD reconciles the rest

## Phases

Phase 1 is what is here, and it is **proven**: traffic reaches the demo backend
through the ALB, the gateway and the ambient mesh, and the platform tears down
cleanly with all of it running — which it did not, for a while.

Phase 2 is Karpenter, and it is proven too: a pod that does not fit gets a node in
about eleven seconds, an idle node is consolidated away, and the platform still
tears down cleanly with Karpenter nodes running — the teardown asks Karpenter to
drain them while its controller is still alive.

Phase 3 is observability, and it is here: the platform reports on itself and the mesh
is visible. Next are External Secrets and Argo Rollouts, once there are applications
to serve. One phase proven before the next, so a failure has one plausible cause
instead of five.
