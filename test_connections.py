r"""Read-only connection checks for the databases in compose.yaml.

Run from PowerShell with:
    .\.venv\Scripts\python.exe .\test_connections.py
"""

from contextlib import closing
import os

import mysql.connector
import psycopg


def check_postgres():
    """Connect over the published Windows port and close on exit."""
    host = os.getenv("PGHOST", "127.0.0.1")
    port = int(os.getenv("PGPORT", "5432"))
    print(f"\nPostgreSQL ({host}:{port})")

    with psycopg.connect(
        host=host,
        port=port,
        dbname=os.getenv("PGDATABASE", "tpcc"),
        user=os.getenv("PGUSER", "labuser"),
        password=os.getenv("PGPASSWORD", "lab_password"),
        connect_timeout=5,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('server_version'), "
                "current_database(), current_user, "
                "current_setting('transaction_isolation')"
            )
            version, database, account, isolation = cursor.fetchone()

    print(f"  Version:   {version}")
    print(f"  Database:  {database}")
    print(f"  Account:   {account}")
    print(f"  Isolation: {isolation}")
    print("  [OK] SQL completed; connection closed.")


def check_mysql():
    """Use closing() to ensure the cursor and connection are released."""
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3307"))
    print(f"\nMySQL ({host}:{port})")

    with closing(mysql.connector.connect(
        host=host,
        port=port,
        database=os.getenv("MYSQL_DATABASE", "tpcc"),
        user=os.getenv("MYSQL_USER", "labuser"),
        password=os.getenv("MYSQL_PASSWORD", "lab_password"),
        connection_timeout=5,
        autocommit=True,
    )) as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT VERSION(), DATABASE(), CURRENT_USER(), "
                "@@session.transaction_isolation, @@default_storage_engine"
            )
            version, database, account, isolation, engine = cursor.fetchone()

    print(f"  Version:   {version}")
    print(f"  Database:  {database}")
    print(f"  Account:   {account}")
    print(f"  Isolation: {isolation}")
    print(f"  Engine:    {engine}")
    print("  [OK] SQL completed; connection closed.")


def main():
    failures = 0
    for check in (check_postgres, check_mysql):
        try:
            check()
        except (psycopg.Error, mysql.connector.Error, ValueError) as error:
            failures += 1
            print(f"  [FAIL] {error}")

    if failures:
        print(f"\nConnection checks failed: {failures}/2")
        return 1

    print("\nBoth databases are reachable from Python on Windows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
