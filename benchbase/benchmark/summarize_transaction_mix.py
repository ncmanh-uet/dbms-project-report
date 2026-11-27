"""Rebuild transaction-mix.csv of an E6 isolation campaign from its request-logs.zip.

Usage: python summarize_transaction_mix.py <request-logs.zip> <output.csv> [<status-output.csv>]

Each worker CSV in the archive has one row per measured request. The output sums
the three repetitions of every DBMS / isolation / client combination and counts,
per transaction type, the requests and those whose final status is SUCCESS.

With the optional third argument the script also writes a long-format table with
one row per DBMS / isolation / client / transaction type / final status / last
SQLSTATE and the number of requests in it. That table shows, for example, that
every PostgreSQL RR Delivery request that did not complete ended in RETRY with
SQLSTATE 40001.
"""
import csv
import io
import re
import sys
import zipfile

TYPES = {"1": "neworder", "2": "payment", "3": "orderstatus", "4": "delivery", "5": "stocklevel"}
ENTRY = re.compile(r"results/(postgres|mysql)/formal-isolation-(rc|rr)-c(\d+)-r\d+-[^/]+/requests/worker-\d+\.csv$")


def main(archive, output, status_output=None):
    counts = {}
    statuses = {}
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
                    key = (dbms, mode, clients, TYPES[row["transaction_type"]], row["status"], row["sqlstate"])
                    statuses[key] = statuses.get(key, 0) + 1
    header = ["dbms", "isolation", "clients"] + [f"{kind}_{suffix}" for kind in TYPES.values() for suffix in ("requests", "successes")]
    with open(output, "w", newline="", encoding="ascii") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for (dbms, mode, clients) in sorted(counts, key=lambda k: (k[0] != "postgres", k[2], k[1])):
            writer.writerow([dbms, mode, clients] + [value for kind in TYPES.values() for value in counts[(dbms, mode, clients)][kind]])
    if status_output:
        with open(status_output, "w", newline="", encoding="ascii") as stream:
            writer = csv.writer(stream)
            writer.writerow(["dbms", "isolation", "clients", "transaction", "final_status", "last_sqlstate", "requests"])
            for key in sorted(statuses, key=lambda k: (k[0] != "postgres", k[2], k[1], list(TYPES.values()).index(k[3]), k[4], k[5])):
                writer.writerow(list(key) + [statuses[key]])


if __name__ == "__main__":
    main(*sys.argv[1:4])
