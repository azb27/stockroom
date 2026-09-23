"""Download the three M5 files we need from a public Hugging Face mirror.

Original source: M5 Forecasting competition (Walmart / University of Nicosia, Kaggle 2020).
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stockroom.config import M5_FILES, M5_HF_REPO, RAW_M5_DIR  # noqa: E402


def main() -> None:
    RAW_M5_DIR.mkdir(parents=True, exist_ok=True)
    for name in M5_FILES:
        dest = RAW_M5_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"skip  {name} (exists)")
            continue
        url = f"https://huggingface.co/datasets/{M5_HF_REPO}/resolve/main/{name}"
        print(f"fetch {url}")
        urllib.request.urlretrieve(url, dest)
        print(f"      -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
