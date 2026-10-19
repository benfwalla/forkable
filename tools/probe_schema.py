"""Discover parts of the Forkable schema without introspection (which is disabled).

graphql-ruby validates queries before executing them, and its validation errors leak
schema details. These probes only send documents that fail validation, so nothing runs.

  python3 tools/probe_schema.py input addPiece       # required fields of AddPieceInput
  python3 tools/probe_schema.py accepts addPiece confirm replacedPieceId
  python3 tools/probe_schema.py field Query myDeliveries
  python3 tools/probe_schema.py vartype myDeliveries from   # finds an argument's type
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forkable.client import GraphQLClient  # noqa: E402


def errors(gql: GraphQLClient, query: str):
    return [e["message"] for e in gql.raw(query, csrf=True).get("errors", [])]


def main():
    gql = GraphQLClient.from_env()
    cmd, *rest = sys.argv[1:] or ["help"]
    if cmd == "input":  # required fields + confirms the mutation exists
        for m in errors(gql, f"mutation {{ {rest[0]}(input: {{__probe: 1}}) {{ errors }} }}"):
            print(m)
    elif cmd == "accepts":  # which of these optional input fields exist
        name, fields = rest[0], rest[1:]
        # One request: graphql-ruby reports every unknown argument at once.
        literal = ", ".join(f"{f}: null" for f in fields)
        msgs = " ".join(errors(gql, f"mutation {{ {name}(input: {{{literal}}}) {{ errors }} }}"))
        for f in fields:
            rejected = f"doesn't accept argument '{f}'" in msgs
            print(f"{f}: {'no' if rejected else 'yes'}")
    elif cmd == "field":  # does Type.field exist? (Query root only, or me {...} for User)
        typ, field = rest
        q = f"{{ {field} }}" if typ == "Query" else f"{{ me {{ {field} }} }}"
        msgs = errors(gql, q)
        print("missing" if any("doesn't exist" in m for m in msgs) else "exists", *msgs, sep="\n  ")
    elif cmd == "vartype":  # bind a deliberately wrong variable type; the error names the real type
        field, arg = rest
        for sel in (" { __typename }", ""):  # object-returning vs scalar-returning fields
            msgs = errors(gql, f"query ($v: Boolean) {{ {field}({arg}: $v){sel} }}")
            mismatch = [m for m in msgs if "Type mismatch" in m]
            if mismatch:
                print(*mismatch, sep="\n")
                break
        else:
            print(*msgs, sep="\n")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
