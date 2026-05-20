# Shoptet feed — kuratované XML pro importpc.cz

## ✅ SAFE TO IMPORT

**`curated_disks.xml`** — 70 SHOPITEM, prošly quality audit (skóre ≥0.50).
  - Jen master_products s vyplněným brand+MPN+form_factor+title+units (qty>0)
  - VAT=0 (§90 margin scheme)
  - VISIBLE=0 (pilot — po importu manuálně schválit)
  - Source: `/home/keni/factory/data/factory.db` master_products
  - Generator: `_tools/shoptet/gen_curated_xml.py`

**`SS51241MSB.xml`** — pilot Kingston A400 240GB M.2 SATA (single product, manually polished).

**`stock_zero_disks.xml`** — 42 disků k odskladnění (qty→0), zastaralé položky.

## ⛔ NEPOUŽÍVAT

**`all_disks_bulk__DRAFT_DO_NOT_IMPORT.xml`** — 182 SHOPITEM z "messy syntézy" 5
data sources, fotky nedůvěryhodné (~50% wrong product), parametry nedůvěryhodné.
Před importem nutný full refactor (viz `_tools/curation/quality_audit.py`).

## Audit

Quality dashboard: https://claudelefi.github.io/importpc-photos/quality_dashboard.html
