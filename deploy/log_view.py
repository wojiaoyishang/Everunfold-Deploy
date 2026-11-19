"""Display structured runtime logs as readable terminal output."""

import json
import sys


def main() -> None:
    for line in sys.stdin:
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if isinstance(item, dict) and all(
            key in item for key in ("time", "level", "component", "message")
        ):
            print(
                " · ".join(str(item[key]) for key in ("time", "level", "component", "message")),
                flush=True,
            )
        else:
            print(line.rstrip("\n"), flush=True)


if __name__ == "__main__":
    main()
