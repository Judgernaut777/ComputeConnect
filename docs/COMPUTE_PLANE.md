# The Compute plane

**ComputeConnect is the Compute plane of the [Connect ecosystem](https://github.com/Judgernaut777/Connect):
compute and provider discovery, resource fit, workload placement, privacy-aware routing,
hardware knowledge, provider availability, and compute cost and capability metadata.**

It **decides where work runs, not how it is computed.** It never loads a tensor, never owns an
engine's lifecycle, and — the point of this document — **Connect never resells, owns, or hosts
the compute.** Independent vendors provide hosting and rented compute; ComputeConnect governs
placement across whatever the customer owns, rents, or contracts.

> **Status: the runtime and privacy model ship; the ownership/billing taxonomy here is design
> direction.** ComputeConnect `0.1.0` ships provider discovery, resource-fit admission,
> placement, and structural default-deny privacy filtering, with single-host heterogeneity
> proven 2026-07-27 and cross-machine placement open. The provider **ownership/billing
> distinctions** below (owned vs rented vs external vs marketplace) are not yet fields on a
> shipped provider — the current provider axis is `placement_class = local | cloud`, a
> privacy/residency axis. This document states the model the plane is built toward; per this
> repo's honesty rule, the gap between it and shipped code is stated plainly.

---

## Connect does not own the compute

The Compute plane is emphatic and repeated across these docs: ComputeConnect does not perform
inference, does not own tasks/memory/tools/engines, and consumes external engines **read-only**
([README](../README.md), [ARCHITECTURE.md](ARCHITECTURE.md)). At ecosystem level this means
**Connect is not a hosting provider, a GPU cloud, or an inference host.** Hosting and rented
compute are **primary marketplace categories** supplied by *independent vendors*; the Compute
plane routes to them, and Connect earns a transaction fee only when it actually facilitates a
paid marketplace purchase — never for the customer's own hardware
([Connect MARKETPLACE_ARCHITECTURE.md](https://github.com/Judgernaut777/Connect/blob/main/MARKETPLACE_ARCHITECTURE.md)).

## Provider ownership and billing distinctions

A compute provider the plane places work on falls into one of four ownership classes. The class
determines who bills and whether a Connect marketplace fee can apply — it never changes how
placement or privacy filtering works.

| Class | Examples | Who bills | Connect marketplace fee | Connect's role |
|---|---|---|---|---|
| **Customer-owned** | Local machine, owned GPU, owned server | No one (it's theirs) | **None, ever** | Govern placement; track operational use if useful; **never** assign a fictional cost |
| **Externally contracted** | A cloud account or GPU contract the customer bought directly | The external provider bills the customer | **None** | Place work; import or accept customer-entered cost data; label incomplete data |
| **Marketplace** | Rented GPU or hosted compute purchased *through* the Connect marketplace | Vendor via the marketplace payment processor | **Disclosed transaction fee** — only because Connect facilitated the purchase | Facilitate the transaction; place work; show the disclosed fee |
| **Free** | A free-tier or community compute option | No one | **None** | Place work; display as free; never manufacture a cost |

**Customer-owned compute is free to govern.** Using a customer's own computer, GPU, or server is
never charged and never assigned a fictional provider charge. The `estimated_cost_usd` a shipped
`/generate` currently returns is a stub (`0.0`); real cost reporting is design direction, and
when it lands it honors this table — owned and free resources show as free.

## Local, owned, rented, external

The `placement_class = local | cloud` axis that ships today is a **privacy/residency** axis
(it gates cloud default-deny), *not* an ownership axis. The ownership classes above are a second,
orthogonal axis the plane is built toward:

- **local** — runs on the customer's device or LAN; typically customer-owned.
- **owned** — customer hardware, wherever it sits.
- **rented** — a marketplace or externally contracted node the customer pays for by usage/time.
- **external** — a provider account or contract the customer holds directly.

A node can be, e.g., `cloud` (residency) **and** externally contracted (ownership) at once. Both
axes are provider metadata; neither turns Connect into the provider.

## Privacy-aware routing and residency

Structural, default-deny privacy is a hard invariant: an absent privacy tier is treated as the
**most restrictive** one, topology is hidden from consumers, and known-but-unhealthy cloud
providers are not even revealed unless cloud is permitted ([ARCHITECTURE.md](ARCHITECTURE.md),
[CONTRACT.md](CONTRACT.md), [adr/0001-privacy-header-body-precedence.md](adr/0001-privacy-header-body-precedence.md)).
Regional and data-residency placement — placing work only on providers approved for a region or
data classification — is design direction over this same machinery
([ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md)).

## Compliance-related provider attributes

Compliance-related provider characteristics (processing/storage regions, retention, deletion,
DPA/BAA/SOC 2 evidence, customer-managed keys) are surfaced as **searchable marketplace metadata**
per framework, not a single "compliant" badge — the field lists live in
[Connect MARKETPLACE_ARCHITECTURE.md](https://github.com/Judgernaut777/Connect/blob/main/MARKETPLACE_ARCHITECTURE.md).
Today the only shipped compliance-adjacent attribute is `placement_class` (`local | cloud`);
the richer per-framework attributes are design direction.

## Cost reporting and budget integration

The plane reports compute cost and capability metadata; it does **not** own budgets. Budget
enforcement against work lives in the Work plane's generalized
[budget model](https://github.com/Judgernaut777/AgentConnect/blob/main/docs/BUDGET_MODEL.md),
which supports arbitrary amounts, intervals, scopes, and delegation. ComputeConnect supplies the
cost/usage signals a budget consumes — and, per the ownership table, supplies **zero** cost for
customer-owned and free providers. Cost reporting itself is currently a stub and is design
direction.

## See also

- [ARCHITECTURE.md](ARCHITECTURE.md) — the objects, the two API layers, structural privacy.
- [CONTRACT.md](CONTRACT.md) — the five binding invariants.
- [ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md) — owned-vs-authorized-use, residency (design direction).
- [STATUS.md](STATUS.md) — what ships today.
- [Connect PRODUCT_THESIS.md](https://github.com/Judgernaut777/Connect/blob/main/PRODUCT_THESIS.md) — where the Compute plane sits, and why Connect does not own the compute.
