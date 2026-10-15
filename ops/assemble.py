"""Concatenate seat prompt parts: assemble.py OUT PART... ; a part 'label=path' gets a '=== label' header."""
import sys

out, parts = sys.argv[1], sys.argv[2:]
with open(out, "w") as f:
    for p in parts:
        label, _, path = p.rpartition("=")
        if label:
            f.write(f"\n=== {label}\n")
        f.write(open(path).read())
print(out, sum(1 for _ in open(out)), "lines")
