"""
Download the face models used by the default engine
===================================================

* YuNet  - face detector        (OpenCV Zoo, MIT licence)
* SFace  - face recognizer      (OpenCV Zoo, Apache-2.0 licence)

Usage:  python scripts/download_models.py [--dir models]
"""

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "face_detection_yunet_2023mar.onnx": f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
MIN_SIZE = 100_000   # bytes: guards against saving an HTML error page / LFS pointer


def download(url: str, target: Path):
    tmp = target.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 16):
            out.write(chunk)
    if tmp.stat().st_size < MIN_SIZE:
        tmp.unlink()
        raise RuntimeError(f"Downloaded file is too small, check the URL: {url}")
    tmp.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "models"))
    args = parser.parse_args()
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, url in MODELS.items():
        target = out_dir / name
        if target.exists() and target.stat().st_size >= MIN_SIZE:
            print(f"[ok]   {name} already present")
            continue
        print(f"[get]  {name}")
        try:
            download(url, target)
        except Exception as exc:
            print(f"[fail] {name}: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"[done] {name} ({target.stat().st_size // 1024} KB)")

    # Sanity check: the models must load in OpenCV
    import cv2
    cv2.FaceDetectorYN.create(str(out_dir / "face_detection_yunet_2023mar.onnx"), "", (320, 320))
    cv2.FaceRecognizerSF.create(str(out_dir / "face_recognition_sface_2021dec.onnx"), "")
    print("Models loaded successfully.")


if __name__ == "__main__":
    main()
