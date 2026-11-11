"""Synthetic demo contract, not a claim about any Kairos product's wire format."""
import json
import sys


def consume(value):
    if not isinstance(value, dict) or set(value) != {"schema", "payload"} or value["schema"] != "kit-demo@1":
        raise ValueError("invalid demo wire")
    return value["payload"]


if __name__ == "__main__":
    try:
        value = json.load(sys.stdin)
        result = {"schema": "kit-demo@1", "payload": value} if sys.argv[1] == "produce" else consume(value)
        print(json.dumps(result))
    except ValueError:
        sys.exit(2)
