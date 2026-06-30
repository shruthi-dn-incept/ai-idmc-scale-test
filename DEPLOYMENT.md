# IDMC Governance Engine — Deployment Guide

## How It Works (No Claude Required)

This application runs as **three standalone Python HTTP servers**. It does NOT require Claude desktop, Claude Code, or any MCP client. The servers use the MCP Python library internally but expose standard HTTP endpoints that the UI calls directly.

The only external AI dependency is the **Anthropic API** — the app calls Claude via API key for the AI steps (taxonomy generation, column curation, DQ rule creation). This is pay-per-use and requires an Anthropic account.

```
Browser
  └── governance_ui.py :8080        (Web UI — FastAPI + HTML)
        ├── ai_governance_mcp.py :8770   (AI steps — calls Anthropic API)
        └── governance_engine_mcp.py :8765  (IDMC/CDQ rules, mappings)
```

---

## Prerequisites

| Requirement | Where to get it |
|---|---|
| Windows 10/11 or Windows Server | — |
| **Python 3.11+** | https://www.python.org/downloads/ — check "Add Python to PATH" during install |
| **Git** | https://git-scm.com/download/win |
| **Anthropic API Key** | https://console.anthropic.com → API Keys → Create key. Used for AI steps (taxonomy, curation, DQ rules). Pay-per-use (~$0.01–$0.10 per pipeline run). |
| **IDMC/CDGC credentials** | From your Informatica org admin |
| **CDQ folder + connection IDs** | From the IDMC environment (see `.env` fields below) |

> **Claude desktop / Claude Code is NOT required.** The servers run fully standalone.

---

## Step 1 — Clone the Repository

Open PowerShell and run:

```powershell
git clone https://github.com/shruthi-dn-incept/ai-idmc-governance-engine-poc.git
cd ai-idmc-governance-engine-poc
```

---

## Step 2 — Get an Anthropic API Key

1. Go to https://console.anthropic.com
2. Sign up or log in
3. Navigate to **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-...`) — you'll need it in the next step

---

## Step 3 — Create the .env File

Copy `.env.example` to `.env` and fill in your credentials:

```powershell
copy .env.example .env
notepad .env
```

Required fields:

```
# Anthropic (for AI steps)
ANTHROPIC_API_KEY=sk-ant-...

# IDMC credentials
IDMC_USER=your.email@company.com
IDMC_PASS=YourPassword
IDMC_LOGIN_HOST=dm-us.informaticacloud.com
IDMC_FRS_HOST=usw3.dm-us.informaticacloud.com
IDMC_DQ_HOST=usw1-dqcloud.dmp-us.informaticacloud.com
IDMC_ORG_ID=your_org_id

# CDGC
CDGC_API_BASE=https://cdgc-api.dm-us.informaticacloud.com

# CDQ
CDQ_FOLDER_ID=your_folder_id
CDQ_FOLDER_NAME=Rule_Specification
IDMC_DQ_CONNECTION_ID=your_connection_id
IDMC_DQ_RUNTIME_ENV_ID=your_runtime_env_id
IDMC_DQ_TEMPLATE_MAPPING_ID=your_template_mapping_id
IDMC_DQ_SCHEMA_PATH=YOUR_SCHEMA/DQ_TEST
```

> **Note:** `IDMC_SESSION_ID`, `IDMC_JWT`, and `IDMC_V3_SESSION_ID` are auto-refreshed at runtime — leave them blank or omit entirely.

---

## Step 4 — Install Dependencies

Open PowerShell **as Administrator** and run:

```powershell
.\deploy\install.ps1
```

This will:
- Install all Python packages from `requirements.txt`
- Open Windows Firewall port 8080 for inbound connections

If you get a script execution error, run this first:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

---

## Step 5 — Start the Servers

```powershell
.\deploy\start_servers.ps1
```

This starts 3 background processes:

| Server | Port | Purpose |
|---|---|---|
| `governance_engine_mcp.py` | 8765 | IDMC/CDQ API tools |
| `ai_governance_mcp.py` | 8770 | AI governance orchestration |
| `governance_ui.py` | 8080 | Web UI |

Logs are written to the `logs\` folder.

---

## Step 6 — Open the UI

Open a browser and go to:

```
http://localhost:8080
```

Or from another machine on the same network:

```
http://<server-ip>:8080
```

---

## Network & Firewall Requirements

### Inbound (open on the server machine)

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 8080 | TCP | Inbound | Web UI — users access the application here |

> Ports 8765 and 8770 are internal-only (localhost communication between the three servers) and do **not** need to be opened in the firewall.

### Outbound (the server must be able to reach these)

| Host | Port | Purpose |
|---|---|---|
| `api.anthropic.com` | 443 (HTTPS) | Anthropic Claude API — AI steps |
| `dm-us.informaticacloud.com` | 443 (HTTPS) | IDMC login / identity service |
| `usw3.dm-us.informaticacloud.com` | 443 (HTTPS) | IDMC FRS / CDGC API |
| `cdgc-api.dm-us.informaticacloud.com` | 443 (HTTPS) | CDGC content & search API |
| `usw1-dqcloud.dmp-us.informaticacloud.com` | 443 (HTTPS) | CDQ Data Quality API |

> All outbound calls use standard HTTPS (port 443) — typically allowed by default.

---

## IT Ticket Template

Use this when raising a ticket to open the firewall port:

---

**Subject:** Request to open inbound TCP port 8080 on Cloud PC — Data Governance POC

**Description:**

We are deploying an internal data governance web application (IDMC CDGC Onboarding Pipeline POC) on the following machine:

- **Machine:** USE-ENT-CPC-ENTRAJ-WIN11 — Shruthi DN
- **IP Address:** 10.122.2.163

The application runs as a Python web server on **port 8080** and needs to be accessible by team members on the internal network for demonstration and evaluation purposes.

**Request:**
Please open **inbound TCP port 8080** on this machine for internal network access.

**Details:**
- Protocol: TCP
- Port: 8080
- Direction: Inbound
- Profile: Domain / Private
- Application: Python web server (governance_ui.py)
- Purpose: Internal POC demo — not exposed to the public internet

No other ports need to be opened. All external API calls (Anthropic, Informatica) go out over standard HTTPS (port 443) which is already permitted.

Please let me know if any additional information is needed.

---

## Stopping the Servers

```powershell
.\deploy\stop_servers.ps1
```

---

## Restarting After a Reboot

Just re-run:

```powershell
.\deploy\start_servers.ps1
```

---

## Troubleshooting

### "python is not recognized"
Python is not on PATH. Re-install Python and check **"Add Python to PATH"**.

### "script cannot be loaded"
Run this once:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### UI loads but AI steps fail (taxonomy, curate, DQ rules)
- Check `ANTHROPIC_API_KEY` is set correctly in `.env`
- Verify the key is active at https://console.anthropic.com

### UI loads but IDMC steps fail
- Check IDMC credentials in `.env`
- Check `logs\ai_governance_mcp_stderr.txt` and `logs\governance_engine_mcp_stderr.txt`

### Port 8080 blocked
Raise a ticket with IT to open inbound TCP port 8080 on the server machine.

### "MCP servers" indicator is red in UI
One or more backend servers crashed. Check the `logs\` folder and re-run `start_servers.ps1`.

---

## Pipeline Steps

| Step | Description | Uses AI |
|---|---|---|
| 1. Discover Catalog | Lists all schemas and tables from CDGC | No |
| 2. Scan Table | Select a table and fetch column metadata | No |
| 3. Generate Taxonomy | Generates domain/subdomain/business-term tree | **Yes** |
| 4. Create Domain Structure | Write taxonomy to CDGC (rename/deselect supported) | No |
| 5. Register System & Dataset | Register source system and dataset in CDGC | No |
| 6. Curate Columns | Link columns to business terms | **Yes** |
| 7. Create DQ Rules | Auto-generate data quality rule specs | **Yes** |
| 8. Propagate Scores | Push DQ scores to CDGC | No |
| 9. Run MCC Scan | Trigger live Snowflake DQ scan via MCC | No |
