#!/usr/bin/python3 -I
"""Development helper: stage the package layout into a private prefix.

Release users install the Debian package with apt; this script is only for
testing the same code paths without root (run with WPT_PREFIX=<prefix>).
"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wpt import WptError
from wpt.deployment import stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    try:
        layout = stage(Path(__file__).resolve().parent, args.prefix)
    except (WptError, OSError) as exc:
        print(f"部署失败：{exc}", file=sys.stderr)
        return 1
    print(f"开发前缀已生成：{layout.prefix}")
    print(f"用法：WPT_PREFIX={layout.prefix} {layout.at('usr/bin/winpe_terminal')} -help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
