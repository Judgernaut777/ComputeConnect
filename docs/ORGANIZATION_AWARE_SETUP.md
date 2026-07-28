# Organization-aware setup — the Compute plane's part

**How ComputeConnect participates when Connect is set up for an organization instead of a single
person.** The organizational model itself — onboarding profiles, import/attach/transfer/federate,
resource ownership — is a Connect management-plane concern, defined in
[Connect's `docs/ORGANIZATION_MODEL.md`](https://github.com/Judgernaut777/Connect/blob/main/docs/ORGANIZATION_MODEL.md).
This document says only what the **Compute plane** owns inside that model.

> **Status: design direction, not shipped.** ComputeConnect `0.1.0` has the primitives this model
> needs — a provider registry, placement policy, and structural default-deny privacy filtering
> (CA-1) — but it has **no organization object, no owner field on a compute node, and no onboarding
> flow.** Read this as the direction the Compute plane converges on, not current runtime; single-host
> heterogeneity is proven and cross-machine placement is still open. [docs/STATUS.md](docs/STATUS.md)
> is the authority on what ships today.

## What the Compute plane configures during org-aware setup

An organization's compute story maps onto the placement and privacy surfaces ComputeConnect already
has:

- **Shared and personal compute** — a node registers the same way whether it belongs to one person or
  a shared department pool. Org-aware setup records *who owns it* and *who may place work on it*; the
  bring-your-own-hosting choices in onboarding (*use this computer* · *connect an existing server* ·
  *connect an existing hosting account*) all resolve to registered providers.
- **Regional and data-residency placement** — the management plane's *geographic and data-residency
  requirements* land here as placement policy over the existing privacy-tier machinery. A workload
  tagged for a region or a data classification must place only on providers approved for it, and the
  **most restrictive tier remains the default when none is given** (CA-1). Organizational scale adds
  regions and residency rules; it never relaxes that default-deny.
- **Multiple hosting and compute environments** — a large organization runs several. Each is a
  provider in the registry; org-aware setup decides which department may place on which.

Two invariants hold at any organizational scale:

- **Privacy is structural and default-deny.** More providers, more regions, and more departments
  never turn placement permissive; an unspecified tier still gets the most restrictive treatment.
- **ComputeConnect decides *where*, not *how*.** It never loads a tensor and never manages an
  engine's lifecycle. Org-aware setup governs placement and residency; it does not make the Compute
  plane an inference engine.

## Ownership vs authorized use

This distinction from the management-plane model is sharpest on the Compute plane, because a personal
machine is the canonical example:

- A **personal workstation** can be *authorized* for approved company tasks while remaining
  individually *owned*. Organization visibility is limited to **availability and approved usage
  only** — never access to personal files, and never a transfer of the node to organization ownership.
- **Externally billed and customer-owned compute is never assigned a fictional cost.** ComputeConnect
  tracks operational use when useful; it never charges for a customer's own hardware. Billing
  ownership travels with the node's ownership metadata (see the management-plane billing table).

On import or federation, a department's **hosting relationships and compute nodes are preserved** —
the parent organization applies broader placement and residency policy on top rather than
re-registering the fleet.

## Boundary

ComputeConnect **decides where work runs, not how it is computed**, at organizational scale too.
Org-aware setup adds regions, residency rules, and shared-vs-personal ownership; it never puts the
Compute plane in the business of running or owning the engines it routes to.

## See also

- [Connect · `docs/ORGANIZATION_MODEL.md`](https://github.com/Judgernaut777/Connect/blob/main/docs/ORGANIZATION_MODEL.md)
  — the full onboarding model this plane plugs into.
- [docs/CONTRACT.md](docs/CONTRACT.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the placement
  interface and the privacy invariant org-scale residency policy builds on.
- [docs/STATUS.md](docs/STATUS.md) — what the Compute plane actually ships today.
