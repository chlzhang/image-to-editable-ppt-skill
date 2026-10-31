import json
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from deck_run_state import resolve_inside, write_json


SCRIPT_DIR = Path(__file__).resolve().parent


def run(command):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True)


def resolve_under_page(page_dir, value):
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return page_dir / path


def imagegen_chroma_helper():
    helper = SCRIPT_DIR / "remove_chroma_key.py"
    if not helper.exists():
        raise SystemExit(f"Missing chroma helper: {helper}")
    return helper


def process_asset_sheet(args, page_dir):
    """Validate/stage all selected steps before replacing page-local outputs."""
    from PIL import Image

    page_dir = Path(page_dir).resolve()
    outputs = {key: resolve_inside(page_dir, getattr(args, key))
               for key in ("chroma", "alpha", "assets_dir", "split_manifest")}
    source = resolve_under_page(page_dir, args.asset_sheet_source) if args.asset_sheet_source else (
        outputs["alpha"] if args.skip_chroma else outputs["chroma"])
    source = source.resolve()
    if not source.is_file():
        raise SystemExit(f"Asset sheet source does not exist: {source}")
    alpha = outputs["alpha"]
    if alpha.exists() and not args.force_chroma and not (source == alpha and args.skip_chroma):
        raise SystemExit(f"Output already exists: {alpha} (use --force-chroma to overwrite)")
    names = [item.strip() for item in (args.asset_names or "").split(",") if item.strip()]
    regions = resolve_under_page(page_dir, args.regions).resolve() if getattr(args, "regions", None) else None
    if regions and not args.skip_split:
        region_data = json.loads(regions.read_text(encoding="utf-8"))
        names.extend(item.get("name", "") for item in region_data.get("regions", []) if isinstance(item, dict))
    for name in names:
        if not isinstance(name, str) or name in {".", ".."} or any(char in name for char in ("/", "\\", ":", "\0")):
            raise SystemExit("Asset names must be safe filename components")
    if any(":" in part for path in outputs.values() for part in path.relative_to(page_dir).parts):
        raise SystemExit("Output paths must not use alternate data streams")
    protected = {(page_dir / name).resolve() for name in ("imagegen-jobs.json", "manifest.json", "page_request.json",
                                                         "page_result.json", "validation.json", "source.png")}
    planned = [outputs["chroma"], alpha, outputs["split_manifest"]]
    if len(set(planned)) != len(planned) or any(path in protected for path in planned):
        raise SystemExit("Asset-sheet outputs must be distinct and must not replace page metadata")

    with tempfile.TemporaryDirectory(prefix=".asset-sheet-", dir=page_dir) as temporary:
        stage = Path(temporary)
        staged = copy.copy(args)
        staged.asset_sheet_source = str(source)
        staged.chroma = str(stage / "chroma.png")
        staged.alpha = str(stage / "alpha.png")
        staged.assets_dir = str(stage / "assets")
        staged.split_manifest = str(stage / "split.json")
        staged.regions = str(regions) if regions else None
        _process_asset_sheet(staged, page_dir)
        with Image.open(staged.alpha) as image:
            if image.convert("RGBA").getchannel("A").getextrema()[1] == 0:
                raise SystemExit("Processed alpha sheet contains no foreground")
        files = [(Path(staged.alpha), alpha)]
        if Path(staged.chroma).is_file():
            files.append((Path(staged.chroma), outputs["chroma"]))
        if not args.skip_split:
            report = json.loads(Path(staged.split_manifest).read_text(encoding="utf-8"))
            if not report.get("assets"):
                raise SystemExit("Asset sheet produced no split assets")
            for item in report["assets"]:
                asset = resolve_inside(staged.assets_dir, item["path"])
                target = resolve_inside(page_dir, outputs["assets_dir"] / asset.name)
                with Image.open(asset) as image:
                    image.verify()
                files.append((asset, target))
                item.update(path=str(target), source=str(alpha))
            report["source"] = str(alpha)
            write_json(staged.split_manifest, report)
            files.append((Path(staged.split_manifest), outputs["split_manifest"]))
        targets = [target for _, target in files]
        if (len({str(path).casefold() for path in targets}) != len(targets)
                or any(path in protected or path == regions for path in targets)
                or any(first in second.parents for first in targets for second in targets if first != second)
                or any(target == getattr(args, "imported_output", None) and target not in {alpha, outputs["chroma"]}
                       for target in targets)):
            raise SystemExit("Asset outputs collide with an input or another output")
        for target in targets:
            if target.exists() and not target.is_file():
                raise SystemExit(f"Output must be a file, not a directory: {target}")
            if any(parent.exists() and not parent.is_dir() for parent in target.parents):
                raise SystemExit(f"Output parent must be a directory: {target}")
        for staged_file, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_file, target)
            print(f"Wrote {target}")
    return {"alpha": str(alpha), "split_manifest": None if args.skip_split else str(outputs["split_manifest"])}


def _process_asset_sheet(args, page_dir):
    chroma = resolve_under_page(page_dir, args.chroma)
    alpha = resolve_under_page(page_dir, args.alpha)
    from PIL import Image

    source = resolve_under_page(page_dir, args.asset_sheet_source) if args.asset_sheet_source else (
        alpha if args.skip_chroma else chroma
    )
    if not source.exists():
        raise SystemExit(f"Asset sheet source does not exist: {source}")
    with Image.open(source) as image:
        extrema = image.convert("RGBA").getchannel("A").getextrema()
    has_alpha = extrema[0] == 0 and extrema[1] > 0
    if extrema[1] == 0:
        raise SystemExit("Asset sheet is fully transparent and contains no foreground")
    if args.skip_chroma and not has_alpha:
        raise SystemExit("--skip-chroma requires real transparent background and nonempty foreground")

    same_alpha = source.resolve() == alpha.resolve()
    if alpha.exists() and not args.force_chroma and not (same_alpha and args.skip_chroma):
        raise SystemExit(f"Output already exists: {alpha} (use --force-chroma to overwrite)")

    if has_alpha:
        alpha.parent.mkdir(parents=True, exist_ok=True)
        if not same_alpha:
            shutil.copy2(source, alpha)
        print(f"Using original alpha: {alpha}")
    else:
        if source.resolve() != chroma.resolve():
            chroma.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, chroma)
        command = [
            sys.executable, imagegen_chroma_helper(), "--input", chroma,
            "--out", alpha, "--auto-key", "border", "--soft-matte",
            "--transparent-threshold", args.transparent_threshold,
            "--opaque-threshold", args.opaque_threshold,
        ]
        if args.despill:
            command.append("--despill")
        if args.force_chroma:
            command.append("--force")
        run(command)

    if args.skip_split:
        return
    if not alpha.exists():
        raise SystemExit(f"Alpha sheet does not exist: {alpha}")

    command = [
        sys.executable,
        SCRIPT_DIR / "split_alpha_components.py",
        "--input",
        alpha,
        "--out-dir",
        resolve_under_page(page_dir, args.assets_dir),
        "--sort",
        args.split_sort,
        "--min-area",
        args.split_min_area,
        "--merge-gap",
        args.split_merge_gap,
        "--merge-union-growth",
        args.split_merge_union_growth,
        "--manifest",
        resolve_under_page(page_dir, args.split_manifest),
    ]
    if getattr(args, "regions", None):
        command.extend(["--regions", resolve_under_page(page_dir, args.regions)])
    if args.square_assets:
        command.append("--square")
    if args.asset_names:
        command.extend(["--names", args.asset_names])
    run(command)


def fit_image(image, size):
    if image.size == size:
        return image
    return image.resize(size)


def write_pair(source_path, preview_path, out_path):
    from PIL import Image, ImageDraw

    source = Image.open(source_path).convert("RGB")
    rebuilt = Image.open(preview_path).convert("RGB")
    source = fit_image(source, rebuilt.size)

    label_h = 32
    gap = 18
    width, height = rebuilt.size
    canvas = Image.new("RGB", (width * 2 + gap, height + label_h), "#f5f7fb")
    canvas.paste(source, (0, label_h))
    canvas.paste(rebuilt, (width + gap, label_h))
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 9), "origin", fill="black")
    draw.text((width + gap + 10, 9), "preview", fill="black")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    print(f"Wrote {out_path}")
