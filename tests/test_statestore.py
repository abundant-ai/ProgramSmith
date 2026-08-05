"""Offline tests for the StateStore seam (Phase 3). LocalFileStore = today's atomic-write behavior
(the only backend — run state is plain local files with atomic publishes)."""


from programsmith.statestore import LocalFileStore, StateStore, get_store, store_for


def test_localfilestore_roundtrip_and_atomicity(tmp_path):
    s = LocalFileStore(tmp_path)
    assert isinstance(s, StateStore)                 # satisfies the protocol
    assert s.read("r/state.json") is None
    assert not s.exists("r/state.json")
    s.write_atomic("r/state.json", '{"stage": "CREATE"}')   # creates parent dirs
    assert s.exists("r/state.json")
    assert s.read("r/state.json") == '{"stage": "CREATE"}'
    assert s.read_bytes("r/state.json") == b'{"stage": "CREATE"}'
    # atomic publish leaves no stray temp files
    assert sorted(p.name for p in (tmp_path / "r").iterdir()) == ["state.json"]
    # overwrite + bytes
    s.write_atomic("r/state.json", b"updated")
    assert s.read("r/state.json") == "updated"


def test_localfilestore_list_and_delete(tmp_path):
    s = LocalFileStore(tmp_path)
    s.write_atomic("a/jobs.json", "{}")
    s.write_atomic("b/jobs.json", "{}")
    s.write_atomic("c/state.json", "{}")
    assert s.list_dir("") == ["a", "b", "c"]          # the fleet scan (runs root → run keys)
    assert s.list_dir("a") == ["jobs.json"]
    assert s.list_dir("missing") == []
    s.delete("a/jobs.json")
    assert not s.exists("a/jobs.json")
    s.delete("a/jobs.json")                            # idempotent (no error if absent)


def test_delete_run_removes_whole_subtree(tmp_path):
    """delete_run wipes a run ENTIRELY — control-plane files AND the nested working tree (task dir,
    source clone, agent logs) — so a dead/contaminated run can be cleared. One rmtree."""
    from programsmith.statestore import delete_run
    runs = tmp_path / "runs"
    run = runs / "awk"
    (run / "task" / "awk" / "environment").mkdir(parents=True)
    (run / "state.json").write_text("{}")
    (run / "jobs.json").write_text("{}")
    (run / "task" / "awk" / "instruction.md").write_text("port ...")
    (runs / "other" / "state.json").parent.mkdir(parents=True)
    (runs / "other" / "state.json").write_text("{}")        # a sibling run must survive
    delete_run(run)
    assert not run.exists()                                  # whole subtree gone
    assert (runs / "other" / "state.json").exists()          # sibling untouched
    # delete_tree on the store is idempotent / safe on a missing path
    LocalFileStore(runs).delete_tree("awk")


def test_get_store_is_local(tmp_path):
    assert isinstance(get_store(tmp_path), LocalFileStore)


def test_store_for_local_maps_to_run_dir(tmp_path):
    """The adoption bridge: store_for(run_dir) + key writes to EXACTLY <run_dir>/<file> —
    byte-identical to the pre-seam path-based code (so existing state is untouched)."""
    run_dir = tmp_path / "runs" / "mykey"
    store, key = store_for(run_dir)
    assert isinstance(store, LocalFileStore) and key == "mykey"
    store.write_atomic(f"{key}/state.json", "X")
    assert (run_dir / "state.json").read_text() == "X"      # == the old run_dir/state.json layout
