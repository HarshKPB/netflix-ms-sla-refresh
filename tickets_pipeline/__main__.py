"""CLI: python -m tickets_pipeline <thread_dir_or_files...> [--out web/tickets.json]

Both the local run and the scheduled cloud routine call this. Point it at a
directory (or list) of Gmail thread dumps; it writes web/tickets.json after the
schema and regression gates pass, or exits non-zero without writing.
"""
import sys
import os
import glob
from . import pipeline


def main(argv):
    args, out, paths = argv[1:], "web/tickets.json", []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out = args[i + 1]
            i += 2
            continue
        if os.path.isdir(a):
            paths += glob.glob(os.path.join(a, "*.json")) + glob.glob(os.path.join(a, "*get_thread*.txt")) \
                + glob.glob(os.path.join(a, "*search_threads*.txt"))
        else:
            paths.append(a)
        i += 1
    if not paths:
        print("usage: python -m tickets_pipeline <thread_dir_or_files> [--out path]", file=sys.stderr)
        return 2
    try:
        rows = pipeline.run(paths, out=out)
    except Exception as e:
        print(f"BUILD FAILED (nothing written): {e}", file=sys.stderr)
        return 1
    from collections import Counter
    c = Counter(r["status"] for r in rows)
    nr = sum(1 for r in rows if r["needs_review"])
    print(f"wrote {out}: {len(rows)} cases {dict(c)} needs_review={nr}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
