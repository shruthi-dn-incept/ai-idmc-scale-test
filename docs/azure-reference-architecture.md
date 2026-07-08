# AI Data Governance Agent on Azure — Reference Architecture

*Prepared for Infrastructure & Security Review · July 2026*

A containerized, batch-mode governance agent that catalogs, curates, and applies
data-quality rules across a Snowflake estate through Informatica IDMC/CDGC —
deployed on fully-managed Azure Container Apps, with no cluster to operate.

| | |
|---|---|
| **Platform** | Azure Container Apps (Jobs) |
| **Region** | East US |
| **Resource group** | `govtest-scale-rg` |

---

## Deployment topology

```
 Trigger                 Registry                Compute                     Governed platform
 Portal / CLI / cron  →  Azure Container   →     ACA Job (scale-to-zero)  →  IDMC / CDGC + Snowflake
 one command or        Registry (ACR)          govtest-scale-job            catalog, glossary,
 a nightly schedule    govtestscaleacr         runs batch, then stops       DQ rules + scores
                       (versioned image)       zero cost when idle          (all access over REST APIs)
```

Secrets (Snowflake key-pair, IDMC creds, LLM key) are injected at runtime from the
Container App secret store / Key Vault — never baked into the image. Egress to
Snowflake and IDMC is over TLS, and the workload can sit behind Entra ID SSO with
VNet integration / private endpoints.

---

## The four questions this answers

**1. Who maintains the infrastructure?** — *No full-time owner.*
Azure Container Apps is managed Kubernetes (AKS + KEDA) under the hood; Microsoft
runs the control plane. Your team never touches a cluster, node pool, or patch
cycle — you own only application config and one agent runtime (see caveat below).

**2. Will it hold at volume?** — *Tiered scale test, extrapolated to 25k.*
The agent runs in tiers (1k → 4k → full catalog), capturing throughput per tier;
the curve extrapolates to the 20–25k-table Aetna data lake. Sizing numbers
accompany this sheet.

**3. What does it cost?** — *You pay while it runs, nothing when idle.*
The ACA Job scales to zero between runs. Cost = container compute per run +
Snowflake credits for DQ execution + the agent VM only while it is scanning.
Per-run figures are in the sizing table.

**4. Is it secure & standards-fit?** — *Secrets managed; SSO & private-net ready.*
No hardcoded credentials — all injected from the secret store / Key Vault. The
deployment fits behind Entra ID SSO and VNet / private endpoints per CVS
enterprise standards.

---

## Components & who operates each

| Component | Azure resource | Role | Operated by |
|---|---|---|---|
| Agent image | `governance-stack` in ACR | Containerized batch agent (scan → curate → DQ) | Microsoft-hosted |
| Batch compute | `govtest-scale-job` (ACA Job) | Runs the catalog/DQ workload, scale-to-zero | Fully managed |
| Interactive UI | `govtest-ui` (ACA app) | Demo / operator console for the pipeline | Fully managed |
| Secrets | Secret store / Key Vault | Snowflake key-pair, IDMC creds, LLM key | Managed |
| DQ execution runtime | `govtest-agent-vm` (Secure Agent) | Runs DQ mappings against Snowflake data | **You manage** |
| Governed platform | IDMC / CDGC + Snowflake | Catalog, glossary, DQ scores, source data | SaaS |

---

## Why ACA Jobs, not AKS — now

- **Zero cluster ops.** ACA runs on managed AKS + KEDA; Microsoft owns the control
  plane. Directly answers "do I need a full-time infra person" — no.
- **Scale-to-zero economics.** A run-then-stop batch job costs nothing idle;
  cheaper than always-on AKS nodes for this workload.
- **Days, not weeks.** No cluster to design, secure, and hand off before the scale test.
- **Nothing throwaway.** ACA runs on AKS underneath — if CVS mandates AKS, the
  **same container image lifts straight over.**

## Data & security flow

- **API-only access.** The agent reaches IDMC/CDGC and Snowflake over authenticated
  REST/TLS — no direct database sprawl.
- **Key-pair to Snowflake.** Service account with RSA key-pair auth; no passwords in transit.
- **Secrets externalized.** Injected at runtime from the secret store; the image carries none.
- **Identity & network.** Deployable behind Entra ID SSO with VNet integration / private endpoints.

---

## One honest note for the infra team

Informatica's **Secure Agent** cannot be serverless — it needs a runtime. Today it
runs on a single self-managed VM (`govtest-agent-vm`, Standard D8s v3), configured
to start unattended on boot and to deallocate when idle. It is the **one**
component your team tends; it can be containerized onto ACA later if desired.
Everything else is fully managed.

---

*Incept Data Solutions · AI-IDMC Governance. Companion document: scale-test sizing
table (throughput · resource envelope · cost).*
