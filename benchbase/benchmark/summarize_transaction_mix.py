"""Rebuild transaction-mix.csv of an E6 isolation campaign from its request-logs.zip.

Usage: python summarize_transaction_mix.py <request-logs.zip> <output.csv>

Each worker CSV in the archive has one row per measured request. The output sums
the three repetitions of every DBMS / isolation / client combination and counts,
per transaction type, the requests and those whose final status is SUCCESS.
"""
import csv
import io
import re
import sys
import zipfile

TYPES = {"1": "neworder", "2": "payment", "3": "orderstatus", "4": "delivery", "5": "stocklevel"}
ENTRY = re.compile(r"results/(postgres|mysql)/formal-isolation-(rc|rr)-c(\d+)-r\d+-[^/]+/requests/worker-\d+\.csv$")


def main(archive, output):
    counts = {}
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            match = ENTRY.search(name)
            if not match:
                continue
            dbms, mode, clients = match.group(1), match.group(2).upper(), int(match.group(3))
            with io.TextIOWrapper(bundle.open(name), encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    total = counts.setdefault((dbms, mode, clients), {key: [0, 0] for key in TYPES.values()})[TYPES[row["transaction_type"]]]
                    total[0] += 1
                    total[1] += row["status"] == "SUCCESS"
    header = ["dbms", "isolation", "clients"] + [f"{kind}_{suffix}" for kind in TYPES.values() for suffix in ("requests", "successes")]
    with open(output, "w", newline="", encoding="ascii") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for (dbms, mode, clients) in sorted(counts, key=lambda k: (k[0] != "postgres", k[2], k[1])):
            writer.writerow([dbms, mode, clients] + [value for kind in TYPES.values() for value in counts[(dbms, mode, clients)][kind]])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
