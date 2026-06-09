#!/usr/bin/env python3
import argparse
import hashlib
from datetime import datetime
from pathlib import Path

from form_mapper import (
    apply_ollama_scalar_fallback,
    build_fill_payload,
    build_layout,
    load_json,
    save_json,
    verify_filled_pdf,
    write_filled_pdf,
)


def file_fingerprint(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill a PDF job form from a general profile JSON with cached layout mapping."
    )
    parser.add_argument("pdf", help="Input PDF path")
    parser.add_argument("--data", default="general_data", help="Path to general data JSON")
    parser.add_argument("--out", default="", help="Output PDF path")
    parser.add_argument("--sector", choices=["general", "public", "private"], default="general")
    parser.add_argument("--mappings-dir", default="mappings", help="Directory for cached mappings")
    parser.add_argument("--logs-dir", default="logs", help="Directory for run logs")
    parser.add_argument("--force-remap", action="store_true", help="Ignore cache and rebuild mapping")
    parser.add_argument("--dry-run", action="store_true", help="Generate mapping/report only, do not write PDF")
    parser.add_argument("--use-ollama-fallback", action="store_true", help="Use local Ollama model for unmapped scalar fields")
    parser.add_argument("--ollama-model", default="gemma4:e4b-it-q8_0", help="Ollama model name for fallback mapping")
    parser.add_argument("--min-confidence", type=float, default=0.86, help="Minimum confidence to accept Ollama mapping")
    parser.add_argument("--ollama-timeout", type=int, default=120, help="Timeout (seconds) for Ollama call")

    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    data_path = Path(args.data).expanduser().resolve()
    mappings_dir = Path(args.mappings_dir).expanduser().resolve()
    logs_dir = Path(args.logs_dir).expanduser().resolve()

    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    if not data_path.exists():
        raise SystemExit(f"Data file not found: {data_path}")

    data = load_json(data_path)

    layout = build_layout(str(pdf_path))
    form_key = layout["fingerprint"]
    cache_path = mappings_dir / f"{form_key}.json"

    if cache_path.exists() and not args.force_remap:
        layout = load_json(cache_path)
        cache_used = True
    else:
        mappings_dir.mkdir(parents=True, exist_ok=True)
        save_json(cache_path, layout)
        cache_used = False

    ollama_result = {
        "enabled": False,
        "status": "disabled",
        "applied_count": 0,
    }
    if args.use_ollama_fallback:
        ollama_result = apply_ollama_scalar_fallback(
            layout=layout,
            data=data,
            model=args.ollama_model,
            min_confidence=args.min_confidence,
            timeout_sec=args.ollama_timeout,
        )
        # Persist updated scalar map so repeated forms improve over time.
        save_json(cache_path, layout)

    sector_hint = None if args.sector == "general" else args.sector
    fill_payload, report = build_fill_payload(layout, data, sector_hint=sector_hint)

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        out_path = pdf_path.with_name(f"{pdf_path.stem} FILLED.pdf")

    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = logs_dir / f"fill-{run_id}.json"

    run_log = {
        "timestamp": datetime.now().isoformat(),
        "pdf": str(pdf_path),
        "pdf_sha1": file_fingerprint(pdf_path),
        "data": str(data_path),
        "mapping_cache": str(cache_path),
        "cache_used": cache_used,
        "output": str(out_path),
        "dry_run": args.dry_run,
        "sector": args.sector,
        "counts": {
            "total_fields": len(layout.get("field_names", [])),
            "total_widgets": len(layout.get("field_instances", [])),
            "filled_fields": len(report.get("filled_fields", [])),
            "filled_widgets": len([v for v in report.get("widget_values", {}).values() if str(v).strip()]),
            "blank_fields": len(report.get("left_blank_fields", [])),
            "employment_rows_detected": len(layout.get("employment_rows", [])),
            "education_rows_detected": len(layout.get("education_rows", [])),
            "reference_slots_detected": len(layout.get("reference_slots", [])),
            "statement_fields_detected": len(layout.get("statement_fields", [])),
            "scalar_mapped_fields": len(layout.get("scalar_map", {})),
            "unmapped_scalar_fields": len(layout.get("unmapped_scalar_fields", [])),
            "ollama_applied_fields": int(ollama_result.get("applied_count", 0) or 0),
        },
        "layout": {
            "fingerprint": layout.get("fingerprint"),
            "field_instances": layout.get("field_instances", []),
            "employment_rows": layout.get("employment_rows", []),
            "education_rows": layout.get("education_rows", []),
            "reference_slots": layout.get("reference_slots", []),
            "statement_fields": layout.get("statement_fields", []),
            "field_contexts": layout.get("field_contexts", {}),
            "scalar_map": layout.get("scalar_map", {}),
            "unmapped_scalar_fields": layout.get("unmapped_scalar_fields", []),
        },
        "fill_report": report,
        "ollama_fallback": ollama_result,
    }

    save_json(log_path, run_log)

    if args.dry_run:
        print(f"DRY RUN complete. Log: {log_path}")
        print(f"Mapping cache: {cache_path} ({'reused' if cache_used else 'created'})")
        print(
            f"Fields: total={run_log['counts']['total_fields']}, "
            f"filled={run_log['counts']['filled_fields']}, blank={run_log['counts']['blank_fields']}"
        )
        if args.use_ollama_fallback:
            print(
                f"Ollama: status={ollama_result.get('status')}, "
                f"applied={ollama_result.get('applied_count', 0)}, "
                f"model={args.ollama_model}"
            )
        return

    widget_payload = report.get("widget_values", {})
    write_filled_pdf(pdf_path, out_path, fill_payload, widget_payload)
    verification = verify_filled_pdf(out_path, fill_payload, widget_payload)
    run_log["verification"] = verification
    save_json(log_path, run_log)

    print(f"Filled PDF: {out_path}")
    print(f"Mapping cache: {cache_path} ({'reused' if cache_used else 'created'})")
    print(f"Run log: {log_path}")
    print(
        f"Fields: total={run_log['counts']['total_fields']}, "
        f"filled={run_log['counts']['filled_fields']}, blank={run_log['counts']['blank_fields']}"
    )
    print(
        f"Verification: checked={verification.get('checked', 0)}, "
        f"matched={verification.get('matched', 0)}, mismatched={verification.get('mismatched', 0)}"
    )
    if args.use_ollama_fallback:
        print(
            f"Ollama: status={ollama_result.get('status')}, "
            f"applied={ollama_result.get('applied_count', 0)}, "
            f"model={args.ollama_model}"
        )


if __name__ == "__main__":
    main()
