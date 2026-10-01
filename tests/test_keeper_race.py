"""The claim race: three concurrent claimants, exactly one winner."""
import threading

from keeper_util import Clock, World, seed
from plangraph.keeper import auth, db as kdb
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


def test_three_threads_one_winner(tmp_path):
    world = World(tmp_path)
    (key,) = seed(world, [("A", [])])
    path = world.path
    workers = [world.tok("workerA"), world.tok("workerB"), world.tok("workerC")]
    barrier = threading.Barrier(len(workers))
    outcomes = []
    lock = threading.Lock()

    def run(token):
        conn = kdb.open_db(path)
        try:
            store = Store(conn, clock=world.clock, verifiers=world.verifiers)
            principal = auth.authenticate(conn, token)
            barrier.wait()
            try:
                result = store.claim(principal, key)
                with lock:
                    outcomes.append(("ok", result["epoch"]))
            except Refused as exc:
                with lock:
                    outcomes.append((exc.code, None))
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(t,)) for t in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [o for o in outcomes if o[0] == "ok"]
    assert len(winners) == 1
    losers = [o[0] for o in outcomes if o[0] != "ok"]
    assert all(code in ("conflict", "bad_state") for code in losers)
    item = world.store._item(key)
    assert item["state"] == "claimed"
    assert item["epoch"] == 1
    world.close()
