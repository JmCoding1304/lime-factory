"""Shared assertion for the walk tests (ticket 251): every Phases row a flow
created carries the stage the owner's table assigns its name."""

from lime.tracer import legacy_stage


def assert_table_stages(con, run_id):
    """Every `phases` row of `run_id` matches the owner's table for its name.

    `con` may be a read-only connection. Row access is by index so it does not
    depend on the connection's row_factory.
    """
    rows = con.execute(
        "SELECT name, stage FROM phases WHERE run_id=? ORDER BY seq", (run_id,)
    ).fetchall()
    assert rows, f"no phases rows for run {run_id!r}"
    for row in rows:
        name, stage = row[0], row[1]
        offending = {name: stage}
        assert stage == legacy_stage(name), f"stage does not follow the owner's table: {offending}"
        assert stage != "Other", f"phase name is unknown to the owner's table: {offending}"
