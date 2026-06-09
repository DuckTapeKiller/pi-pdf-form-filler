# Local PDF Form Autofill

## Files

- `index.ts` — Pi tool wrapper for `fill_pdf_form`
- `fill_any_form.py` — CLI entrypoint
- `form_mapper.py` — PyMuPDF/pypdf field discovery, visual layout analysis, mapping, filling, flattening, and verification
- `mappings/` — cached per-form mapping layout (by field fingerprint)
- `logs/` — run logs (what was detected and filled)
- `general_data` — master profile data used for forms

## Use from Pi

Run the tool from Pi:

```text
/tool fill_pdf_form {"pdf":"~/.pi/agent/extensions/pdf-form-filler/Application Form.pdf","dryRun":true,"useAiMapping":true}
```

Recommended workflow for unfamiliar forms:

1. Run with `dryRun=true` and `useAiMapping=true`.
2. Inspect the latest JSON log in `logs/`.
3. Re-run without `dryRun` after the mapping looks right.
4. Check the verification summary; a trusted run should report `mismatched=0`.

## CLI usage

```bash
cd "~/.pi/agent/extensions/pdf-form-filler"
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data --out "Application Form AUTOFILL.pdf"
```

## Dry run (no PDF written)

```bash
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data --dry-run
```

## Rebuild mapping cache

```bash
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data --force-remap
```

## Ollama fallback for unmapped scalar fields (local model)

```bash
./venv/bin/python fill_any_form.py "Application Form.pdf" \
  --data general_data \
  --use-ollama-fallback \
  --ollama-model "gemma4:e4b-it-q8_0" \
  --min-confidence 0.86
```

Use `--dry-run` first if you want to inspect suggested mappings before writing a PDF.

The AI mapper receives:

- raw PDF field names
- field type and dropdown options
- PDF tooltip text
- nearby page text and visual widget geometry from PyMuPDF
- allowed profile data paths and short sample values

It can only map to approved paths from `general_data`; uncertain mappings are rejected below the confidence threshold.

## Sector-specific statement selection

```bash
# default from general_data -> statement_selection_rules.default
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data

# force public-sector statement
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data --sector public

# force private-sector statement
./venv/bin/python fill_any_form.py "Application Form.pdf" --data general_data --sector private
```

## Reliability model

1. Deterministic field discovery from PDF widgets
2. Rich field context extraction (tooltip, options, nearby text, geometry)
3. Widget-instance targeting so duplicate field names can receive different values
4. Section mapping (employment / education / references)
5. Typed date formatting, including compact DOB fields such as `DDMMYY`
6. Optional local Ollama mapping for unclear scalar fields
7. Visual flattening so filled values render reliably even in broken AcroForms
8. Post-fill verification from the flattened PDF text layer
9. Mapping cache reuse for repeat form types
10. Run log for audit/debug

## Notes

- Keep your master data in `general_data`.
- For new form types, run once with `--dry-run` and inspect `logs/*.json`.
- If a form has broken field names, use `--force-remap` after updating `form_mapper.py` rules or after accepting better AI mappings.
