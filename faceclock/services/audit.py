"""
Tamper-evident audit log
========================

Each entry stores ``hash = sha256(prev_hash + payload)``. Editing or deleting
any past row breaks the chain, which ``verify_chain()`` detects.
"""

import hashlib
import json

from ..db import execute, get_db, now_iso, query

GENESIS = "0" * 64


def _digest(prev_hash, ts, actor, action, entity, entity_id, details):
    payload = json.dumps([prev_hash, ts, actor, action, entity, entity_id, details],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit(actor, action, entity=None, entity_id=None, details=None):
    """Append an entry to the audit log."""
    last = query("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1", one=True)
    prev_hash = last["hash"] if last else GENESIS
    ts = now_iso()
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    digest = _digest(prev_hash, ts, actor, action, entity, entity_id, details_json)
    execute(
        "INSERT INTO audit_log (ts, actor, action, entity, entity_id, details, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, str(actor), action, entity, entity_id, details_json, prev_hash, digest),
    )


def verify_chain():
    """Return ``(ok, first_broken_id)`` after re-computing every hash."""
    prev_hash = GENESIS
    for row in get_db().execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _digest(prev_hash, row["ts"], row["actor"], row["action"],
                           row["entity"], row["entity_id"], row["details"])
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return False, row["id"]
        prev_hash = row["hash"]
    return True, None
