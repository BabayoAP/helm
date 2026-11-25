"""M0 exit: journal append/replay (I4, SEC-7)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from helm.core.enums import ControlAction, EngineState, OrderEventKind, OrderState
from helm.core.events import ControlCommand, EngineStateChanged, OrderEvent
from helm.oms.journal import Journal
from tests.factories import intent, order


def test_append_and_replay_roundtrip(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    events = [
        intent(),
        OrderEvent(
            kind=OrderEventKind.ACCEPTED,
            client_order_id="r1-s1-0",
            state=OrderState.ACCEPTED,
            order=order(),
            ts=5,
        ),
        ControlCommand(action=ControlAction.KILL, source="ui", ts=6),
        EngineStateChanged(
            previous=EngineState.RUNNING, current=EngineState.HALTED, reason="kill", ts=7
        ),
    ]
    for i, ev in enumerate(events):
        j.append(ev, ts=i, run_id="r1", account_id="paper", key="k")
    j.close()

    j2 = Journal(tmp_path / "j.sqlite")
    assert list(j2.replay()) == events
    assert j2.last_seq() == 4
    assert [type(e).__name__ for e in j2.replay(kinds=("ControlCommand",))] == ["ControlCommand"]
    assert list(j2.replay(after_seq=3)) == events[3:]
    j2.close()


def test_jsonl_mirror_matches_sqlite(tmp_path: Path) -> None:
    with Journal(tmp_path / "j.sqlite") as j:
        j.append(intent(), ts=1, run_id="r", account_id="a")
        j.append(intent(qty="0.02"), ts=2, run_id="r", account_id="a")
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    assert [json.loads(x)["seq"] for x in lines] == [1, 2]
    assert json.loads(lines[1])["payload"]["qty"] == "0.02"


@pytest.mark.parametrize("sql", ["UPDATE journal SET kind='x'", "DELETE FROM journal"])
def test_journal_is_append_only(tmp_path: Path, sql: str) -> None:
    with Journal(tmp_path / "j.sqlite") as j:
        j.append(intent(), ts=1, run_id="r", account_id="a")
    db = sqlite3.connect(tmp_path / "j.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(sql)


def test_filter_by_account_and_key(tmp_path: Path) -> None:
    with Journal(tmp_path / "j.sqlite", mirror=False) as j:
        j.append(intent(), ts=1, run_id="r", account_id="a", key="x")
        j.append(intent(), ts=2, run_id="r", account_id="b", key="y")
        assert [e.account_id for e in j.entries(account_id="b")] == ["b"]
        assert [e.key for e in j.entries(key="x")] == ["x"]
        assert j.count() == 2
