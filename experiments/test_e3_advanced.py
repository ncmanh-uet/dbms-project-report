"""Offline regression tests for accounting; no database or Docker calls."""
import csv
import json
from decimal import Decimal
from pathlib import Path
import tempfile
from queue import Queue
from threading import Event, BoundedSemaphore
import unittest
from unittest.mock import patch

from e3_advanced import FIELDS, summarize, init_shard, worker_shard


class AccountingTests(unittest.TestCase):
    def fixture(self, directory):
        rows=[
            ["warmup",0,0,1,"OK",1,0,0,0,0,.01,10,""],
            ["measure",0,0,1,"OK",2,1,0,0,0,.2,200,""],
            ["measure",0,1,1,"EXHAUSTED",9,9,0,0,.2,1.1,900,""],
        ]
        with (directory/"worker-0.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.writer(f); w.writerow(FIELDS); w.writerows(rows)
        sample=dict(phase="measure",generator_cpu_cores=.2,generator_rss_bytes=1000,lock_waiters=0,docker={"CPUPerc":"20%"})
        (directory/"samples.jsonl").write_text(json.dumps(sample)+"\n")
        (directory/"sampler.json").write_text('{"errors":[]}')
        return dict(dbms="postgres",method="occ",distribution="hot1",clients=1,repetition=1,measure_seconds=1)

    def run_accounting(self, expected_value):
        # summarize records paths relative to the workspace.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as name:
            directory=Path(name)
            case=self.fixture(directory)
            return summarize(directory,case,1.1,{1:Decimal(0)},{1:(Decimal(expected_value),3)})

    def test_retry_and_warmup_are_not_extra_successes(self):
        r=self.run_accounting(2)
        self.assertEqual(r["status"],"PASS")
        self.assertEqual(r["successes"],1)
        self.assertEqual(r["attempts"],11)
        self.assertEqual(r["version_conflicts"],10)
        self.assertEqual(r["completion_pct"],50)
        self.assertEqual(r["exhausted_pct"],50)
        self.assertEqual(r["p95_ms"],200)
        self.assertAlmostEqual(r["successful_tps"],1/1.1)
        self.assertEqual(r["integrity"]["successes_including_warmup"],2)

    def test_lost_update_fails_integrity(self):
        r=self.run_accounting(1)
        self.assertEqual(r["status"],"FAIL")
        self.assertEqual(len(r["integrity"]["mismatches"]),1)

    def test_connection_error_is_reported_to_coordinator(self):
        with tempfile.TemporaryDirectory() as name:
            q=Queue()
            init_shard(q,{"warmup":Event(),"measure":Event()},[0,0,0,0],BoundedSemaphore(8))
            with patch("e3_advanced.connect",side_effect=RuntimeError("expected connection failure")):
                with self.assertRaisesRegex(RuntimeError,"expected connection failure"):
                    worker_shard([0],"postgres","unused",{},name)
            event,cid,error=q.get_nowait()
            self.assertEqual((event,cid),("error",0))
            self.assertIn("expected connection failure",error)
            self.assertTrue((Path(name)/"worker-0-error.json").exists())


if __name__=="__main__":
    unittest.main()
