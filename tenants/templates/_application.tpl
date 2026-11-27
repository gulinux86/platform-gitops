{{- /*
An Application under the apps project, deploying from app-gitops into one tenant
namespace. Up to three sources from the same repository: the shared service chart,
the values file it is rendered with (`ref: values` is what `$values` names), and
plain manifests that are not a service.

The release name is the resource name: Deployment, Service, PDB, HPA and HTTPRoute
are all named after it. app-gitops' CI renders every values file under the same
name and namespace (apps/<app>/values.yaml -> <app>, apps/<app>/<svc>/values.yaml
-> <app>-<svc>, in app-<app>).

The finalizer makes deleting the tenant delete what the Application created;
without it the objects are orphaned.
*/ -}}
{{- define "tenants.application" }}
{{- $repo := .root.Values.appRepo.url }}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {{ .app }}
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: apps
  sources:
    {{- if .values }}
    - repoURL: {{ $repo }}
      targetRevision: {{ .revision }}
      path: charts/service
      helm:
        releaseName: {{ .release }}
        valueFiles:
          - $values/{{ .values }}
    - repoURL: {{ $repo }}
      targetRevision: {{ .revision }}
      ref: values
    {{- end }}
    {{- if .manifests }}
    - repoURL: {{ $repo }}
      targetRevision: {{ .revision }}
      path: {{ .manifests }}
    {{- end }}
  destination:
    server: https://kubernetes.default.svc
    namespace: {{ .ns }}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
{{- end }}
