#!/usr/bin/env python3
import argparse
from pathlib import Path

from deck_run_state import now_iso, read_json, resolve_inside, write_json
from _page_artifacts import process_asset_sheet as process_sheet


def configure_default_paths(args):
    if args.job_id:
        job_id = args.job_id
        if any(value is None for value in (args.chroma, args.alpha, args.split_manifest)):
            if (
                "\x00" in job_id
                or "/" in job_id
                or "\\" in job_id
                or job_id in {".", ".."}
                or Path(job_id).is_absolute()
                or Path(job_id).name != job_id
            ):
                raise SystemExit("--job-id must be a safe filename component when default output paths are used")
        args.chroma = args.chroma or f"assets/{job_id}.asset-sheet-chroma.png"
        args.alpha = args.alpha or f"assets/{job_id}.asset-sheet-alpha.png"
        args.split_manifest = args.split_manifest or f"assets/{job_id}.split-report.json"
    else:
        args.chroma = args.chroma or "imagegen_asset_sheet_chroma.png"
        args.alpha = args.alpha or "imagegen_asset_sheet_alpha.png"
        args.split_manifest = args.split_manifest or "split_assets.json"


def mark_processed(page_dir, args):
    if not args.job_id:
        return
    jobs_path = page_dir / "imagegen-jobs.json"
    jobs = read_json(jobs_path, default={"schema_version": 1, "jobs": []})
    for item in jobs.get("jobs", []):
        if item.get("job_id") == args.job_id:
            item.update(
                {
                    "status": "processed",
                    "processed_at": now_iso(),
                    "alpha": args.alpha,
                    "assets_dir": args.assets_dir,
                    "split_manifest": args.split_manifest,
                }
            )
            break
    else:
        jobs.setdefault("jobs", []).append(
            {
                "job_id": args.job_id,
                "role": "asset_sheet",
                "status": "processed",
                "processed_at": now_iso(),
                "alpha": args.alpha,
                "assets_dir": args.assets_dir,
                "split_manifest": args.split_manifest,
            }
        )
    jobs["updated_at"] = now_iso()
    write_json(jobs_path, jobs)


def main():
    parser = argparse.ArgumentParser(
        prog="editppt image process-sheet",
        description="Preserve transparent input or remove chroma key, then split an asset sheet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  editppt image process-sheet <page_dir> --job-id icon-sheet-1 --asset-sheet-source assets/sheet.png --asset-names icon-a,icon-b
""",
    )
    parser.add_argument("page_dir", help="Page directory that owns imagegen-jobs.json, manifest.json, and the assets folder.")
    parser.add_argument("--job-id", help="Image generation job id to mark as processed after splitting.")
    parser.add_argument("--asset-sheet-source", help="Read-only sheet input; may be external. Defaults to the imported job output, or its recorded alpha with --skip-chroma.")
    parser.add_argument("--chroma", help="Intermediate chroma-key output inside page_dir (relative or absolute). Defaults to a job-scoped path when --job-id is set.")
    parser.add_argument("--alpha", help="Transparent sheet output inside page_dir (relative or absolute). Defaults to a job-scoped path when --job-id is set.")
    parser.add_argument("--skip-chroma", action="store_true", help="Skip chroma-key removal when processing an already-transparent sheet.")
    parser.add_argument("--force-chroma", action="store_true", help="Allow replacing an existing alpha output; transparent input always preserves its original alpha.")
    parser.add_argument("--despill", action="store_true", help="Reduce remaining chroma color around extracted asset edges.")
    parser.add_argument("--skip-split", action="store_true", help="Do not auto-split connected alpha components.")
    parser.add_argument("--transparent-threshold", default="12", help="RGB distance threshold treated as fully transparent during chroma removal.")
    parser.add_argument("--opaque-threshold", default="220", help="RGB distance threshold treated as fully opaque during chroma removal.")
    parser.add_argument("--assets-dir", default="assets", help="Output directory for split assets inside page_dir (relative or absolute).")
    parser.add_argument("--regions", help="Read-only JSON object regions in generated-sheet pixels; relative to page_dir or an external absolute path.")
    parser.add_argument("--asset-names", help="Comma-separated names assigned to split assets in visual order.")
    parser.add_argument("--split-sort", choices=["x", "y", "area"], default="x", help="Sort extracted alpha components by x position, y position, or area.")
    parser.add_argument("--split-min-area", default="1000", help="Minimum connected-component pixel area to keep as an extracted asset.")
    parser.add_argument("--split-merge-gap", default="18", help="Merge nearby components separated by at most this many pixels.")
    parser.add_argument("--split-merge-union-growth", default="2.4", help="Maximum bounding-box growth ratio allowed when merging nearby components.")
    parser.add_argument("--square-assets", action="store_true", help="Pad split assets to square canvases.")
    parser.add_argument("--split-manifest", help="JSON report inside page_dir (relative or absolute). Defaults to a job-scoped path when --job-id is set.")
    args = parser.parse_args()
    configure_default_paths(args)

    page_dir = Path(args.page_dir).resolve()
    if not page_dir.exists():
        raise SystemExit(f"Page folder does not exist: {page_dir}")
    args.imported_output = None
    if args.job_id:
        jobs = read_json(page_dir / "imagegen-jobs.json", default={"jobs": []})
        job = next((item for item in jobs.get("jobs", []) if item.get("job_id") == args.job_id), None)
        if job and job.get("output"):
            args.imported_output = resolve_inside(page_dir, job["output"])
        if not args.asset_sheet_source:
            source = None
            if job:
                source = (job.get("alpha") if args.skip_chroma else None) or job.get("output")
            if not source:
                raise SystemExit(f"No imported output for job {args.job_id}; supply --asset-sheet-source explicitly")
            args.asset_sheet_source = str(resolve_inside(page_dir, source))
    process_sheet(args, page_dir)
    mark_processed(page_dir, args)


if __name__ == "__main__":
    main()
