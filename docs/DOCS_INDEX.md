# IDMC Documentation Index

> Use this index to find the right documentation for each API/feature.
> All docs are in the `docs/` folder of this project.

## Immediate Priority (Building Governance Engine)

| Doc | File | Size | What It Covers |
|-----|------|------|----------------|
| **IICS REST API Reference** | `IICS_November2025_(REST-API)Reference_en.pdf` | 6.1M | v2 API for mappings, tasks, taskflows, connections, schedules, runtime environments. THE main API reference for CDI operations. |
| **CDGC API Reference** | `DGC_2025July_(API)Reference_en.pdf` | 2.6M | Create/update/delete assets, search catalog, export, import, DQ scores upload, audit history. Critical for register_in_cdgc tool. |
| **CDI Mappings** | `CDI_October2025_Mappings_en.pdf` | 4.0M | Mapping design, sources, targets, transformations, DQ transformations. Needed for generate_dq_mapping tool. |
| **CDI Taskflows** | `CDI_October2025_Taskflows_en.pdf` | 3.4M | Taskflow creation, scheduling, dependencies. Needed for schedule_mapping tool. |
| **CDI Tasks** | `CDI_October2025_Tasks_en.pdf` | 3.1M | Task types, mapping tasks, configuration. |
| **Rule Specification Assets** | `IICS_October2025_RuleSpecificationAssets_en.pdf` | 1.4M | CDQ rule spec creation, management, rule logic. Validates our reverse-engineered CDQ API. |

## Secondary (Other MCP Servers)

| Doc | File | Size | Which Server |
|-----|------|------|--------------|
| **CDQ API Reference** | `CDQ_2025October_(API)Reference_en.pdf` | 336K | Dictionary API (Governance Engine) |
| **DQ for Assets in CDGC** | `DGC_2025July_DataQualityForAssets_en.pdf` | 3.4M | DQ Monitor |
| **Data Marketplace API** | `DMP_2025November_(API)Reference_en.pdf` | 4.2M | Data Onboarding |
| **Data Profiling** | `CDP_October2025_DataProfiling_en.pdf` | 3.0M | Data Onboarding |
| **Business Assets** | `DGC_2025November_UnderstandingBusinessAssets_en.pdf` | 1.5M | Glossary Manager |
| **Asset Management** | `DGC_2025November_AssetManagement_en.pdf` | 6.7M | Lineage Reporter |
| **Asset Discovery** | `DGC_2025November_AssetDiscovery_en.pdf` | 14M | Lineage Reporter |

## Key API Patterns (Discovered + Documented)

### CDQ (Cloud Data Quality)
- **CDQ Cloud UI:** `usw1-dqcloud.dmp-us.informaticacloud.com`
- **CDQ API (dictionaries):** `usw1-dqcloud-api.dmp-us.informaticacloud.com/quality/public/v2/dictionaries`
- **Rule Service:** `usw1-dqcloud.dmp-us.informaticacloud.com/rule-service/api/v1/`
- **FRS (metadata):** `usw1.dmp-us.informaticacloud.com/frs/api/v1/Documents`
- **Auth:** IDS-SESSION-ID header

### CDI (Cloud Data Integration)
- **v2 API:** `usw1.dmp-us.informaticacloud.com/saas/api/v2/` (see IICS REST API Reference)
- **Endpoints:** /mapping, /mttask, /taskflow, /connection, /runtimeEnvironment, /schedule
- **Auth:** icSessionId header

### CDGC (Cloud Data Governance and Catalog)
- **API Host:** `cdgc-api.dmp-us.informaticacloud.com`
- **Model API:** `/ccgf-modelv2/api/v2/`
- **MCP Server:** CDGC Metadata Search (search_metadata, get_asset_details)
- **Auth:** IDS-SESSION-ID header or Bearer JWT
- See DGC API Reference for create/update/search endpoints

### Data Marketplace
- **API:** See DMP API Reference
- **MCP Server:** Data Provisioning (checkout_data, list_data_collections)
