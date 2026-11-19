"""Tests for phase filtering, units and outcome/aggregation semantics."""
import unittest
import analyze_results as analysis


class AnalysisTests(unittest.TestCase):
    def test_measurement_excludes_warmup_and_drain(self):
        rows = [dict(utc=f"2026-11-10T00:00:0{i}+00:00", phase=phase, sessions="10",
                     lock_waiting_sessions=str(waiters), sample_ms="1")
                for i, (phase, waiters) in enumerate([
                    ("warmup", 10), ("measure", 2), ("measure", 4), ("drain", 10)])]
        result = analysis.lock_metrics(rows)
        self.assertEqual(result["db_samples_measure"], 2)
        self.assertEqual(result["mean_lock_waiting_sessions"], 3)
        self.assertEqual(result["mean_waiting_session_pct"], 30)

    def test_binary_memory_and_cpu_quota(self):
        self.assertEqual(analysis.memory_bytes("1MiB"), 1048576)
        self.assertEqual(analysis.memory_bytes("1MB"), 1000000)
        rows = []
        for role in ("dbms-lab-postgres-1", "labbench-postgres-run"):
            for phase, cpu in (("warmup", "0%"), ("measure", "200%")):
                rows.append(dict(utc="2026-11-10T00:00:00+00:00", phase=phase,
                                 stats=dict(Name=role, CPUPerc=cpu, MemUsage="128MiB / 2GiB")))
        metrics = analysis.resource_metrics(rows, db_cpus=2, gen_cpus=4)
        self.assertEqual(metrics["db_cpu_quota_pct_mean"], 100)
        self.assertEqual(metrics["generator_cpu_quota_pct_mean"], 50)
        self.assertEqual(metrics["db_memory_mib_mean"], 128)

    def test_retry_and_abort_are_different_outcomes(self):
        rows = [dict(transaction_type=str(kind), attempts=str(attempts), status=status,
                     phase="1", start_ns="1000000", end_ns="11000000",
                     retry_events=str(attempts-1), sqlstate="", sqlcode="0")
                for kind, attempts, status in [(1, 1, "USER_ABORTED"),
                    (4, 1, "USER_ABORTED"), (2, 2, "SUCCESS")]]
        metrics = analysis.request_metrics(rows)
        self.assertEqual(metrics["aborted"], {1: 1, 4: 1})
        self.assertEqual(metrics["statuses"]["SUCCESS"], 1)
        self.assertEqual(metrics["retry_requests"], 1)
        self.assertEqual(metrics["reexecutions"], 1)

    def test_percentile_uses_linear_interpolation(self):
        self.assertAlmostEqual(analysis.quantile([1, 2, 101], .95), 91.1)

    def test_summary_is_per_run_and_requires_three_repetitions(self):
        rows = []
        for db in analysis.DBMS:
            for repetition, value in enumerate([10, 100, 1000], 1):
                row = dict(dbms=db, clients=1, repetition=repetition)
                row.update({key: value for key in analysis.METRICS})
                row.update({key: 1 for key in analysis.COUNTS})
                rows.append(row)
        summary = analysis.summarize(rows, [1])
        self.assertEqual(summary[0]["latency_p95_success_ms_mean"], 370)
        self.assertEqual(summary[0]["latency_p95_success_ms_min"], 10)
        self.assertEqual(summary[0]["latency_p95_success_ms_max"], 1000)
        with self.assertRaisesRegex(ValueError, "Missing or duplicate"):
            analysis.summarize(rows[:-1], [1])


if __name__ == "__main__":
    unittest.main()
