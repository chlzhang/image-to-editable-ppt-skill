#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from deck_run_state import recover_run_state, run_lock

from deck_run_state import load_deck, load_jobs, now_iso, resolve_inside, run_dir_from_target, save_deck, save_jobs, set_run_status, sha256_file, write_json


SCRIPT_DIR = Path(__file__).resolve().parent


def run(command):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True)


def final_output_path(run_dir, deck):
    output = Path(deck.get("output", "final/deck_edited.pptx"))
    if output.is_absolute():
        return output
    return run_dir / output


def assert_pages_ready(run_dir, jobs):
    problems = []
    for page in jobs.get("pages", []):
        if page.get("status") not in {"recorded", "accepted"}:
            problems.append(f"{page['page_id']} status={page.get('status')}")
            continue
        result = page.get("result") or {}
        if result.get("validation_passed") is not True:
            problems.append(f"{page['page_id']} validation_passed={result.get('validation_passed')}")
        outputs = result.get("outputs") or {}
        hashes = result.get("hashes") or {}
        if not outputs or set(outputs) != set(hashes):
            problems.append(f"{page['page_id']} missing recorded output hashes; validate and record the page again")
            continue
        files = [(outputs[key], digest) for key, digest in hashes.items()]
        files.extend((result.get("asset_hashes") or {}).items())
        for relative_path, expected_hash in files:
            path = Path(run_dir) / relative_path
            if not path.is_file() or sha256_file(path) != expected_hash:
                problems.append(
                    f"{page['page_id']} recorded artifact changed or missing: {relative_path}; "
                    "repair affected outputs, validate and record the page again"
                )
    if problems:
        raise SystemExit("Pages are not ready for finalize:\n" + "\n".join(problems))


def recorded_page_entries(run_dir: Path, deck: dict, jobs: dict) -> list[dict]:
    """Bind deck order to the manifests and validation reports that record verified."""
    pages = deck.get("pages", [])
    job_pages = jobs.get("pages", [])
    deck_ids = [page.get("page_id") for page in pages]
    job_ids = [page.get("page_id") for page in job_pages]
    if (not pages or any(not isinstance(value, str) or not value for value in deck_ids + job_ids)
            or len(set(deck_ids)) != len(deck_ids) or len(set(job_ids)) != len(job_ids)
            or set(deck_ids) != set(job_ids) or deck.get("page_count", len(pages)) != len(pages)):
        raise SystemExit("Deck and recorded jobs must have matching unique page ids and page counts")
    by_id = {page["page_id"]: page for page in job_pages}
    entries = []
    for index, page in enumerate(pages, 1):
        job = by_id[page["page_id"]]
        if page.get("page_index", index) != index or job.get("page_index", index) != index:
            raise SystemExit(f"Page order mismatch for {page['page_id']}")
        page_dir = resolve_inside(run_dir, job["page_dir"])
        if resolve_inside(run_dir, page["page_dir"]) != page_dir:
            raise SystemExit(f"Page directory mismatch for {page['page_id']}")
        result = job.get("result") or {}
        outputs = result.get("outputs") or {}
        hashes = result.get("hashes") or {}
        paths = {}
        for key in ("page_manifest", "validation"):
            if not outputs.get(key) or not hashes.get(key):
                raise SystemExit(f"{page['page_id']} missing recorded {key}; validate and record the page again")
            path = resolve_inside(page_dir, resolve_inside(run_dir, outputs[key]))
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != hashes[key]:
                raise SystemExit(f"{page['page_id']} recorded artifact changed: {path}")
            paths[key] = (path, json.loads(data))
        entries.append({"manifest": paths["page_manifest"][1],
                        "manifest_path": paths["page_manifest"][0],
                        "validation_path": paths["validation"][0]})
    return entries


def main():
    parser = argparse.ArgumentParser(description="Build and validate the final editable PPTX from recorded pages.")
    parser.add_argument("run", help="Run directory or deck_manifest.json")
    args = parser.parse_args()

    run_dir = run_dir_from_target(args.run)
    with run_lock(run_dir):
        recover_run_state(run_dir)
        execute(run_dir, args)


def execute(run_dir, args):
    deck = load_deck(run_dir)
    jobs = load_jobs(run_dir)
    assert_pages_ready(run_dir, jobs)
    entries = recorded_page_entries(run_dir, deck, jobs)

    out = final_output_path(run_dir, deck)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Keep the selected input mapping inspectable and use it for BOTH commands.
    # Re-reading the original deck here would silently restore unrecorded defaults.
    assembly = dict(deck, job_dir=str(run_dir), pages=[
        dict(page, manifest=str(entry["manifest_path"]), validation=str(entry["validation_path"]))
        for page, entry in zip(deck["pages"], entries)
    ])
    assembly_path = out.with_suffix(".assembly.json")
    write_json(assembly_path, assembly)
    run([sys.executable, SCRIPT_DIR / "build_pptx_from_manifest.py", "--deck-manifest", assembly_path, "--out", out])
    set_run_status(run_dir, "deck_built", "final pptx built")

    validation = out.parent / "validation.json"
    run([sys.executable, SCRIPT_DIR / "validate_pptx.py", out, "--deck-manifest", assembly_path, "--report", validation])
    set_run_status(run_dir, "deck_validated", "final pptx validation passed")

    for page in jobs.get("pages", []):
        page["status"] = "accepted"
        page["accepted"] = True
        page["accepted_at"] = now_iso()
    jobs["run_status"] = "complete"
    jobs["updated_at"] = now_iso()
    save_jobs(run_dir, jobs)
    deck["completed_at"] = now_iso()
    save_deck(run_dir, deck)
    summary = {
        "schema_version": 1,
        "run_id": deck.get("run_id"),
        "status": "complete",
        "page_count": len(jobs.get("pages", [])),
        "output": str(out),
        "validation": str(validation),
        "completed_at": now_iso(),
    }
    write_json(out.parent / "run_summary.json", summary)
    set_run_status(run_dir, "complete", "final deck complete")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
