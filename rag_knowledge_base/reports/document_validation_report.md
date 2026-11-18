# Document validation report
Generated 2026-11-17T13:01:10+00:00

- Registry rows: **22**  |  eligible for core ingestion: **12**  |  excluded: **10**
- Reference-only files present (never ingested): **8**

## Included documents
| ID | Format | Words | Extraction status | Missing key metadata | Unknown (non-critical) |
|---|---|---|---|---|---|
| NIMH_001 | HTML | 599 | OK (trafilatura) | - | publication_date, licence |
| NIMH_002 | HTML | 1944 | OK (trafilatura) | - | publication_date, last_updated_date |
| NIMH_003 | HTML | 1900 | OK (trafilatura) | - | publication_date, last_updated_date |
| NIMH_004 | HTML | 2152 | OK (trafilatura) | - | publication_date, last_updated_date |
| NIMH_005 | HTML | 923 | OK (trafilatura) | - | publication_date, last_updated_date |
| NIMH_006 | HTML | 633 | OK (trafilatura) | - | publication_date |
| NIMH_007 | HTML | 679 | OK (trafilatura) | - | publication_date, last_updated_date |
| NIMH_008 | HTML | 1717 | OK (trafilatura) | - | publication_date |
| WHO_001 | HTML | 1238 | OK (trafilatura) | - | publication_date, last_updated_date, licence |
| WHO_002 | HTML | 693 | OK (trafilatura) | - | publication_date, last_updated_date, licence |
| WHO_003 | PDF | 12366 | OK (132 pages, PyMuPDF; check column/heading order manually) | - | publication_date, last_updated_date |
| WHO_004 | HTML | 1610 | OK (trafilatura) | - | publication_date, last_updated_date, licence |

## Excluded documents
| ID | Reason |
|---|---|
| NIMH_007_PDF | identical text to NIMH_007 (same underlying file/content) |
| WHO_003_PAGE | metadata-only landing page (the PDF is the document) |
| WHO_005 | reference-only material (never ingested) |
| WHO_006 | reference-only material (never ingested) |
| WHO_007 | reference-only material (never ingested) |
| WHO_008 | reference-only material (never ingested) |
| WHO_009 | reference-only material (never ingested) |
| WHO_010 | reference-only material (never ingested) |
| WHO_011 | reference-only material (never ingested) |
| WHO_012 | reference-only material (never ingested) |

## Problems needing your attention
- **WHO_011**: original file not found on disk
- **WHO_005**: extraction `Pending` -> manual correction/OCR needed
- **WHO_006**: extraction `Pending` -> manual correction/OCR needed
- **WHO_007**: extraction `Pending` -> manual correction/OCR needed
- **WHO_008**: extraction `Pending` -> manual correction/OCR needed
- **WHO_009**: extraction `Pending` -> manual correction/OCR needed
- **WHO_010**: extraction `Pending` -> manual correction/OCR needed
- **WHO_011**: extraction `Pending` -> manual correction/OCR needed
- **WHO_012**: extraction `Pending` -> manual correction/OCR needed

## Reference-only files
- reference_only/intervention_manuals/WHO_Group_Problem_Management_Plus_Training_Manual.html
- reference_only/intervention_manuals/WHO_WHO_Problem_Management_Plus_PM_Intervention_Manual.html
- reference_only/intervention_manuals/WHO_WHO_Problem_Management_Plus_PM_Training_Manual.html
- reference_only/intervention_manuals/WHO_WHO_Psychological_Interventions_Implementation_Manual.html
- reference_only/intervention_manuals/WHO_WHO_Psychological_Interventions_Resource_Directory.html
- reference_only/intervention_manuals/WHO_WHO_Psychological_Self_Help_Interventions_Manual_2026.html
- reference_only/intervention_manuals/WHO_WHO_Self_Help_Plus_SH_Course_Manual.html
- reference_only/intervention_manuals/WHO_WHO_Self_Help_Plus_SH_Facilitator_Training_Manual.html

## Metadata completeness note
Publication/last-updated dates and licences are auto-detected by collect_kb.py and often `Unknown`. They are carried into chunk metadata as null when unknown - nothing is invented.
