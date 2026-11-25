"""Build a minimal, auditable Worker patch against the pinned image JARs.

Prerequisites: original profile JARs and postgres/lib copied out of the images;
JDK 24 at --jdk. No SQL procedure or transaction selection changes.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
ROOT = HERE.parents[1]
COMMIT = "33c00473807ebd49304d114a6d769d2d2b2bbb34"
SOURCE = f"https://raw.githubusercontent.com/cmu-db/benchbase/{COMMIT}/src/main/java/com/oltpbenchmark/api/Worker.java"
EXPECTED_JARS = {
    "postgres": "8948beb84382a65311d6e297c6804d9690511fa2955e2bbae8c20b58a4904a12",
    "mysql": "59e5bc51b76db32c236aabd1032732aa26ebbe5a2f78bed02906dee1f6fa59a1",
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError(f"Source anchor does not match exactly once: {before[:100]}")
    return text.replace(before, after, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jdk", default=r"C:\Program Files\Java\jdk-24")
    parser.add_argument("--verify-isolation", action="store_true")
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    (RUNTIME / "lib").mkdir(exist_ok=True)
    for profile, expected in EXPECTED_JARS.items():
        jar = RUNTIME / f"benchbase-{profile}.jar"
        if not jar.exists() or (profile == "postgres" and not list((RUNTIME / "lib").glob("*.jar"))):
            script = f"cp /benchbase/profiles/{profile}/benchbase.jar /export/benchbase-{profile}.jar"
            if profile == "postgres":
                script += " && cp /benchbase/profiles/postgres/lib/*.jar /export/lib/"
            subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), "run", "--rm", "--no-deps",
                            "--entrypoint", "sh", "-v", f"{RUNTIME}:/export", f"benchbase-{profile}",
                            "-c", script], cwd=ROOT, check=True)
        if digest(jar) != expected:
            raise RuntimeError(f"{profile} original JAR differs from the pinned image")
    vendor = RUNTIME / "vendor/com/oltpbenchmark/api/Worker.java"
    vendor.parent.mkdir(parents=True, exist_ok=True)
    if not vendor.exists():
        urllib.request.urlretrieve(SOURCE, vendor)
    original = vendor.read_text(encoding="utf-8")
    source_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if source_sha256 != "4463226c5cbf0fb544325b1dc8250fd4ebdcde7fe18d29e589a42272e880a61d":
        raise RuntimeError("Upstream Worker source hash differs from the reviewed source")
    patched = replace_once(original, "  private final int id;", """  // Lab instrumentation: only record requests within the native latency window.
  private TransactionStatus labStatus = TransactionStatus.UNKNOWN;
  private int labAttempts, labRetryEvents, labSqlCode;
  private String labSqlState = "";
  private final int id;""")
    anchor = "              latencies.addLatency(transactionType.getId(), start, end, this.id, prePhase.getId());"
    patched = replace_once(patched, anchor, anchor + """
              com.oltpbenchmark.lab.RequestMetrics.record(this.id, transactionType.getId(),
                  prePhase.getId(), start, end, labStatus.name(), labAttempts,
                  labRetryEvents, labSqlState, labSqlCode);""")
    anchor = "  protected final void doWork(DatabaseType databaseType, TransactionType transactionType) {"
    patched = replace_once(patched, anchor, anchor + """
    labStatus = TransactionStatus.UNKNOWN;
    labAttempts = labRetryEvents = labSqlCode = 0;
    labSqlState = "";""")
    anchor = "      while (retryCount < maxRetryCount && this.workloadState.getGlobalState() != State.DONE) {"
    patched = replace_once(patched, anchor, anchor + "\n        labAttempts++;")
    anchor = "          // check if we should attempt to ignore connection errors and reconnect"
    patched = replace_once(patched, anchor, """          labSqlState = ex.getSQLState() == null ? "" : ex.getSQLState();
          labSqlCode = ex.getErrorCode();
""" + anchor)
    anchor = '              LOG.debug(String.format("%s failed to open a connection...", this));'
    patched = replace_once(patched, anchor, anchor)  # also verify this source version
    anchor = "            retryCount++;\n            continue;"
    patched = replace_once(patched, anchor, """            labStatus = TransactionStatus.ERROR;
            labSqlState = ex.getSQLState() == null ? "" : ex.getSQLState();
            labSqlCode = ex.getErrorCode();
            retryCount++;
            continue;""")
    anchor = "          switch (status) {"
    patched = replace_once(patched, anchor, """          labStatus = status;
          if (status == TransactionStatus.RETRY || status == TransactionStatus.RETRY_DIFFERENT) {
            labRetryEvents++;
          }
""" + anchor)
    patched = replace_once(patched, '    return errorCode == 0 && sqlState.equals("40001");',
                           '    return errorCode == 0 && (sqlState.equals("40001") || sqlState.equals("40P01"));')
    output_dir = RUNTIME / "isolation" if args.verify_isolation else RUNTIME
    if args.verify_isolation:
        anchor = "this.conn.setTransactionIsolation(this.configuration.getIsolationMode());"
        assert patched.count(anchor) == 2
        patched = patched.replace(anchor, anchor + """
            int observedIsolation = this.conn.getTransactionIsolation();
            if (observedIsolation != this.configuration.getIsolationMode()) {
              throw new SQLException("Unexpected JDBC transaction isolation: " + observedIsolation);
            }
            this.conn.rollback();
            System.out.printf("E6 verified isolation worker=%d jdbc=%d%n", this.id, observedIsolation);""")
    source = output_dir / "src/com/oltpbenchmark/api/Worker.java"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(patched, encoding="utf-8", newline="\n")
    (output_dir / "Worker.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(True), patched.splitlines(True), "upstream/Worker.java", "lab/Worker.java")), encoding="utf-8")
    classes = output_dir / "classes"
    classes.mkdir(exist_ok=True)
    helper = HERE / "java/com/oltpbenchmark/lab/RequestMetrics.java"
    subprocess.run([str(Path(args.jdk) / "bin/javac.exe"), "--release", "23", "-cp",
                    str(RUNTIME / "benchbase-postgres.jar") + ";" + str(RUNTIME / "lib/*"),
                    "-d", str(classes), str(source), str(helper)], check=True)
    manifest = {"upstream_commit": COMMIT, "source_url": SOURCE, "source_sha256": source_sha256,
                "patched_source_sha256": digest(source), "patch_sha256": digest(output_dir / "Worker.patch"),
                "helper_sha256": digest(helper), "profiles": {}}
    original_workers = []
    for profile in ("postgres", "mysql"):
        src = RUNTIME / f"benchbase-{profile}.jar"
        target = output_dir / f"benchbase-{profile}-instrumented.jar"
        additions = {p.relative_to(classes).as_posix(): p for p in classes.rglob("*.class")}
        with zipfile.ZipFile(src) as archive, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
            original_workers.append(hashlib.sha256(archive.read("com/oltpbenchmark/api/Worker.class")).hexdigest())
            for entry in archive.infolist():
                if entry.filename not in additions:
                    output.writestr(entry, archive.read(entry))
            for name, path in additions.items():
                output.write(path, name)
        manifest["profiles"][profile] = {"original_jar_sha256": digest(src), "instrumented_jar_sha256": digest(target)}
    if len(set(original_workers)) != 1:
        raise RuntimeError("Profile Worker classes differ; a shared patch is unsafe")
    manifest["original_worker_class_sha256"] = original_workers[0]
    (output_dir / "instrumentation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
