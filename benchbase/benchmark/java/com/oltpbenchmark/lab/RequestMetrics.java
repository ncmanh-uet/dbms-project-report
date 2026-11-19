/* Local benchmark instrumentation, Apache-2.0. No workload SQL changes. */
package com.oltpbenchmark.lab;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public final class RequestMetrics {
  private static final List<BufferedWriter> WRITERS = new ArrayList<>();
  private static final ThreadLocal<BufferedWriter> LOCAL = new ThreadLocal<>();
  static {
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      synchronized (WRITERS) {
        for (BufferedWriter writer : WRITERS) {
          try { writer.close(); }
          catch (IOException error) { error.printStackTrace(); Runtime.getRuntime().halt(74); }
        }
      }
    }, "lab-metrics-flush"));
  }

  public static void record(int worker, int transactionType, int phase, long start, long end,
                            String status, int attempts, int retries, String sqlState, int sqlCode) {
    try {
      BufferedWriter writer = LOCAL.get();
      if (writer == null) {
        String directory = System.getProperty("lab.metrics.dir");
        if (directory == null) throw new IOException("Missing -Dlab.metrics.dir");
        Files.createDirectories(Path.of(directory));
        writer = new BufferedWriter(Files.newBufferedWriter(Path.of(directory, "worker-" + worker + ".csv")), 65536);
        writer.write("worker,transaction_type,phase,start_ns,end_ns,status,attempts,retry_events,sqlstate,sqlcode\n");
        synchronized (WRITERS) { WRITERS.add(writer); }
        LOCAL.set(writer);
      }
      writer.write(worker + "," + transactionType + "," + phase + "," + start + "," + end + ","
          + status + "," + attempts + "," + retries + "," + sqlState + "," + sqlCode + "\n");
    } catch (IOException error) {
      throw new IllegalStateException("Request metric output failed", error);
    }
  }
}
