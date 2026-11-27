#!/usr/bin/env python3
"""Checks every tenant against the shape the platform admits applications in, and
against the app-gitops repository it points at.

Tenants are rendered from the chart in tenants/ — one file per application under
tenants/registry/ — and this reads the rendered output, which is what Argo CD
applies. The chart already refuses a malformed registry file (unknown key, wrong
file name, missing team); what is checked here is what the chart cannot see: the
app-gitops files each Application names, and claims across tenants.

A tenant that breaks one of these is accepted by Git and refused, or silently
wrong, in the cluster: the `apps` AppProject rejects a namespace outside app-*, a
values file that is not on the branch makes the Application fail to render, and a
route from a namespace without `gateway-access` is never attached by the gateway.

Per tenant (registry/<name>.yaml, rendered):
  - exactly one Namespace, named app-<x>, labelled into the ambient mesh;
  - at least one Application, named like the Namespace or after it
    (<namespace>-<service>), in argocd, under `project: apps`, deploying to that
    Namespace in this cluster, with the resources finalizer (deleting the tenant
    deletes what it created) and automated sync;
  - every source from app-gitops, every valueFiles entry and manifests path present
    there at the source's targetRevision;
  - a chart source's releaseName is the one app-gitops' layout gives that values
    file — apps/<app>/values.yaml → <app>, apps/<app>/<svc>/values.yaml →
    <app>-<svc> — in namespace app-<app>; app-gitops' CI renders it under the same;
  - `gateway-access: "true"` on the Namespace when a values file sets a route.
Across tenants: no Namespace, Application or release claimed twice.

Usage: scripts/ci/check_tenants.py <app-gitops checkout> <rendered tenants.yaml>
"""

import pathlib
import re
import subprocess
import sys

import yaml

APP_GITOPS = "https://github.com/gulinux86/app-gitops"
CHART_PATH = "charts/service"
NAMESPACE = re.compile(r"^app-[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
VALUES = re.compile(r"^\$values/(apps/([a-z0-9-]+)(?:/([a-z0-9-]+))?/values\.yaml)$")
FINALIZER = "resources-finalizer.argocd.argoproj.io"
IN_CLUSTER = "https://kubernetes.default.svc"


class AppGitops:
    """Reads files from the app-gitops checkout at any revision a source names."""

    def __init__(self, path):
        self.path = path

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, check=False)

    def _rev(self, revision):
        for candidate in (f"origin/{revision}", revision):
            if self._git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").returncode == 0:
                return candidate
        return None

    def read(self, revision, path):
        rev = self._rev(revision)
        if rev is None:
            return None
        shown = self._git("show", f"{rev}:{path}")
        return shown.stdout if shown.returncode == 0 else None

    def has_yaml_under(self, revision, path):
        rev = self._rev(revision)
        if rev is None:
            return False
        listed = self._git("ls-tree", "--name-only", f"{rev}:{path}")
        return listed.returncode == 0 and any(n.endswith((".yaml", ".yml")) for n in listed.stdout.split())


def check_application(where, app, namespace, repo, errors, claims):
    meta, spec = app.get("metadata") or {}, app.get("spec") or {}
    name = meta.get("name", "?")
    where = f"{where}: Application/{name}"

    if name != namespace and not name.startswith(f"{namespace}-"):
        errors.append(f"{where} — name it {namespace} or {namespace}-<service>")
    if meta.get("namespace") != "argocd":
        errors.append(f"{where} — metadata.namespace must be argocd")
    if spec.get("project") != "apps":
        errors.append(f"{where} — project must be apps (got {spec.get('project')!r})")
    if FINALIZER not in (meta.get("finalizers") or []):
        errors.append(f"{where} — missing finalizer {FINALIZER}: deleting the tenant would orphan its resources")
    destination = spec.get("destination") or {}
    if destination.get("namespace") != namespace:
        errors.append(f"{where} — destination.namespace must be {namespace}")
    if destination.get("server") != IN_CLUSTER:
        errors.append(f"{where} — destination.server must be {IN_CLUSTER}")
    automated = (spec.get("syncPolicy") or {}).get("automated") or {}
    if not (automated.get("prune") and automated.get("selfHeal")):
        errors.append(f"{where} — syncPolicy.automated needs prune and selfHeal, like every Application here")
    claims.setdefault(("Application", name), []).append(where)

    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    if not sources:
        errors.append(f"{where} — no source")
    refs = {s["ref"]: s for s in sources if s.get("ref")}
    routed = False

    for source in sources:
        url = (source.get("repoURL") or "").removesuffix(".git")
        revision = source.get("targetRevision") or "HEAD"
        if url != APP_GITOPS:
            errors.append(f"{where} — source {url!r} is not {APP_GITOPS}")
            continue
        if source.get("ref"):
            continue
        path = source.get("path", "")
        if path == CHART_PATH:
            helm = source.get("helm") or {}
            release = helm.get("releaseName")
            value_files = helm.get("valueFiles") or []
            if not value_files:
                errors.append(f"{where} — the chart source has no valueFiles")
            for entry in value_files:
                match = VALUES.match(entry)
                if not match:
                    errors.append(f"{where} — valueFiles entry {entry!r} is not $values/apps/<app>[/<svc>]/values.yaml")
                    continue
                file, app_name, service = match.groups()
                ref = refs.get("values")
                if ref is None:
                    errors.append(f"{where} — {entry} uses $values but no source has ref: values")
                    continue
                text = repo.read(ref.get("targetRevision") or "HEAD", file)
                if text is None:
                    errors.append(
                        f"{where} — {file} is not in app-gitops at {ref.get('targetRevision')!r}: "
                        "merge the application's values there first"
                    )
                    continue
                expected = f"{app_name}-{service}" if service else app_name
                if release != expected:
                    errors.append(f"{where} — releaseName must be {expected!r} for {file} (got {release!r})")
                if namespace != f"app-{app_name}":
                    errors.append(f"{where} — {file} belongs in namespace app-{app_name}, not {namespace}")
                values = yaml.safe_load(text) or {}
                if ((values.get("route") or {}).get("path")):
                    routed = True
            claims.setdefault(("release", namespace, release), []).append(where)
        elif path:
            # A tenant copied from another keeps that one's manifests path, which
            # exists — and would deploy the other application's objects here.
            own = f"apps/{namespace.removeprefix('app-')}/"
            if not path.startswith(own):
                errors.append(f"{where} — {path} is another application's; this tenant's manifests live under {own}")
            elif not repo.has_yaml_under(revision, path):
                errors.append(f"{where} — {path} has no manifests in app-gitops at {revision!r}")
        else:
            errors.append(f"{where} — a source with neither path nor ref")
    return routed


def check_tenant(where, docs, repo, errors, claims):
    namespaces = [d for d in docs if d.get("kind") == "Namespace"]
    applications = [d for d in docs if d.get("kind") == "Application"]

    if len(namespaces) != 1:
        errors.append(f"{where} — needs exactly one Namespace (found {len(namespaces)})")
        return
    ns = namespaces[0].get("metadata") or {}
    namespace = ns.get("name", "")
    labels = ns.get("labels") or {}
    if not NAMESPACE.match(namespace):
        errors.append(f"{where} — Namespace {namespace!r} must be app-<name>: the apps project deploys nowhere else")
    if labels.get("istio.io/dataplane-mode") != "ambient":
        errors.append(f"{where} — Namespace needs istio.io/dataplane-mode: ambient")
    claims.setdefault(("Namespace", namespace), []).append(where)

    for doc in docs:
        kind = doc.get("kind")
        if kind not in ("Namespace", "Application") and (doc.get("metadata") or {}).get("namespace") != namespace:
            errors.append(f"{where}: {kind}/{(doc.get('metadata') or {}).get('name')} — must be in namespace {namespace}")

    if not applications:
        errors.append(f"{where} — no Application")
    routed = False
    for app in applications:
        routed |= check_application(where, app, namespace, repo, errors, claims)
    if routed and labels.get("gateway-access") != "true":
        errors.append(f"{where} — a values file sets a route, so the Namespace needs gateway-access: \"true\"")


def tenant_of(doc):
    """The tenant namespace a rendered object belongs to."""
    if doc.get("kind") == "Namespace":
        return (doc.get("metadata") or {}).get("name", "")
    if doc.get("kind") == "Application":
        return ((doc.get("spec") or {}).get("destination") or {}).get("namespace", "")
    return (doc.get("metadata") or {}).get("namespace", "")


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    repo = AppGitops(sys.argv[1])
    rendered = pathlib.Path(sys.argv[2])
    errors, claims = [], {}
    grouped = {}
    for doc in yaml.safe_load_all(rendered.read_text()):
        if isinstance(doc, dict):
            grouped.setdefault(tenant_of(doc), []).append(doc)
    tenants = sorted(grouped)
    for namespace in tenants:
        where = f"tenants/registry/{namespace.removeprefix('app-')}.yaml"
        print(f"== {where}")
        check_tenant(where, grouped[namespace], repo, errors, claims)
    for key, owners in claims.items():
        if len(owners) > 1:
            errors.append(f"{' '.join(str(k) for k in key)} is claimed by {len(owners)}: {', '.join(owners)}")
    for error in errors:
        print(f"::error::{error}")
    print(f"{len(tenants)} tenant(s), {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
