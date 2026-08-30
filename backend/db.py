import os
import psycopg2
import psycopg2.extras

DSN = os.environ["TM35_DSN"]


def get_conn():
    conn = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def query(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
    finally:
        conn.close()


def execute_returning(sql, params=None):
    """INSERT/UPDATE ... RETURNING — в отличие от query(), коммитит.
    query()/query_one() — только для чтения, коммита у них нет намеренно."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def run_in_transaction(fn):
    """fn(cur) — несколько операций в одной транзакции, один commit."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            result = fn(cur)
        conn.commit()
        return result
    finally:
        conn.close()
