"""The guard that decides which database the suite may destroy.

`migrated_db` drops and recreates the `public` schema. That has already
erased a live schema once, so this file exists to prove the refusal still
works — and, since the test database now lives on the same managed host as
the live one, that a host check alone is no longer what is doing the work.
"""

from __future__ import annotations

import pytest

from tests.conftest import _database_of, _is_disposable

LIVE = "postgresql://u:p@ep-example-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
LIVE_DIRECT = "postgresql://u:p@ep-example.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
TEST_REMOTE = "postgresql://u:p@ep-example-pooler.ap-southeast-1.aws.neon.tech/tracker_test"
LOCAL = "postgresql://postgres:pg@127.0.0.1:5433/tracker_test"


def test_the_live_database_is_never_disposable():
    assert _is_disposable(LIVE, LIVE) is False


def test_the_live_database_is_refused_through_its_direct_endpoint_too():
    """Neon serves the pooled and direct endpoints from hostnames differing
    only by '-pooler'. Same cluster, same rows, so both must be refused."""
    assert _is_disposable(LIVE_DIRECT, LIVE) is False
    assert _is_disposable(LIVE, LIVE_DIRECT) is False


def test_a_remote_database_is_accepted_only_when_its_name_says_disposable():
    assert _is_disposable(TEST_REMOTE, LIVE) is True


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@ep-example.ap-southeast-1.aws.neon.tech/neondb",
        "postgresql://u:p@ep-example.ap-southeast-1.aws.neon.tech/production",
        "postgresql://u:p@db.example.com/analytics",
    ],
)
def test_a_remote_database_without_the_suffix_is_refused(dsn):
    """This is the rule doing the work now that host is no longer decisive."""
    assert _is_disposable(dsn, LIVE) is False


def test_a_local_database_stays_disposable_whatever_it_is_called():
    assert _is_disposable(LOCAL, LIVE) is True
    assert _is_disposable("postgresql://postgres:pg@localhost/postgres", LIVE) is True


def test_a_local_database_is_still_refused_if_it_is_the_live_one():
    local_live = "postgresql://postgres:pg@localhost/neondb"
    assert _is_disposable(local_live, "postgresql://postgres:pg@localhost/neondb") is False


def test_a_dsn_with_no_database_name_is_refused():
    assert _is_disposable("postgresql://u:p@ep-example.aws.neon.tech/", LIVE) is False


def test_the_database_name_is_read_off_the_dsn():
    assert _database_of(LIVE) == "neondb"
    assert _database_of(TEST_REMOTE) == "tracker_test"
