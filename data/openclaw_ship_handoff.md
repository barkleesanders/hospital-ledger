# OpenClaw Ship Handoff

Generated: 2026-05-11T22:59:39+00:00
Runner status: /Users/barkleesanders/projects/hospital-ledger/data/macmini_openclaw_pipeline.status.json
Runner log: /Users/barkleesanders/projects/hospital-ledger/data/macmini_openclaw_pipeline.log
Finalizer status: /Users/barkleesanders/projects/hospital-ledger/data/finalize_ship_status.json

Deploy was intentionally left for OpenClaw /ship.

Suggested OpenClaw prompt:

```
/goal Finish Hospital Ledger after the Mac mini local parser/R2 upload run. Read data/macmini_openclaw_pipeline.status.json and data/finalize_ship_status.json. If the runner phase is error, use /carmack to fix the failing script or API call. If upload completed and deploy is still pending, use /ship for the hospital-ledger Pages site, then verify https://hospital-ledger.pages.dev/data/summary.json and /api/prices-index.
```
