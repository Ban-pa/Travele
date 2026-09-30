"""把 Travele/P 里的地点照片按名字挂到两份数据上（可重跑）。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHOTO_DIR = ROOT / "P"
SPOTS_FILE = ROOT / "static" / "data" / "spots.json"
CATALOG_FILE = ROOT / "data" / "destinations.json"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def photo_key(filename: str) -> str:
    """比对用的归一化名字：去扩展名、去空格与点号，再去掉文件名里混进来的 "jpg" 这种尾巴。"""
    stem = pathlib.PurePath(filename).stem
    stem = re.sub(r"(jpg|jpeg|png|webp|gif|avif)$", "", stem, flags=re.I)
    return re.sub(r"[\s·・_\-—()（）]", "", stem)


def build_index() -> dict[str, str]:
    index: dict[str, str] = {}
    if not PHOTO_DIR.is_dir():
        return index
    for path in sorted(PHOTO_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            index[photo_key(path.name)] = path.name
    return index


def photo_for(name: str, index: dict[str, str]) -> str | None:
    target = photo_key(name)
    if not target:
        return None
    if target in index:
        return index[target]
    for image_key, filename in index.items():
        if target in image_key or image_key in target:
            return filename
    return None


def main(write: bool) -> None:
    index = build_index()
    print(f"照片目录 {PHOTO_DIR}：{len(index)} 张\n")

    spots = json.loads(SPOTS_FILE.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    places = catalog["places"]

    spot_hits, place_hits = 0, 0
    for spot in spots:
        filename = photo_for(spot["spot"], index)
        spot["photo"] = filename
        spot_hits += bool(filename)
        print(f"  照片墙 {'✔' if filename else '✗'} {spot['spot']:<16} {filename or ''}")
    for place in places:
        filename = photo_for(place["name"], index)
        if filename:
            place["photo"] = filename
            place_hits += 1

    print(f"\nspots.json 命中 {spot_hits}/{len(spots)}｜destinations.json 命中 {place_hits}/{len(places)}")
    if not write:
        print("\n（这次只是看看，没写文件；要写就加 --write）")
        return

    SPOTS_FILE.write_text(json.dumps(spots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写回 {SPOTS_FILE.name} 与 {CATALOG_FILE.name}")


if __name__ == "__main__":
    main("--write" in sys.argv)
