"""An in-memory fake standing in for ``opensearchpy.OpenSearch`` in unit
tests -- implements just the surface ``OpenSearchKnowledgeBackend`` calls
(``info``/``cluster.health``/``indices.*``/``get``/``index``/``delete``/
``delete_by_query``/``exists``/``bulk``/``search``/``count``), enough to
exercise real request/response shapes without needing a live cluster or
even ``opensearch-py`` installed (this module has no such import).

``FakeOpenSearch`` optionally injects failures for a bounded number of
attempts per document id, to exercise ``opensearch_bulk``'s retry path.
"""

from __future__ import annotations

import copy
from typing import Any


class _Indices:
    def __init__(
        self,
        store: dict[str, dict[str, dict[str, Any]]],
        mappings: dict[str, dict[str, Any]],
    ) -> None:
        self._store = store
        self._mappings = mappings

    def exists(self, index: str) -> bool:
        return index in self._store

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self._store.setdefault(index, {})
        self._mappings[index] = copy.deepcopy(body.get("mappings", {}))
        return {"acknowledged": True}

    def refresh(self, index: str) -> dict[str, Any]:
        return {"_shards": {"total": 1}}

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": self._mappings.get(index, {})}}

    def put_mapping(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        props = self._mappings.setdefault(index, {}).setdefault("properties", {})
        props.update(body.get("properties", {}))
        return {"acknowledged": True}


class _Cluster:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise ConnectionError("cluster unreachable")
        return {"status": "green"}


class FakeOpenSearch:
    """``store``: index -> doc_id -> source. ``fail_ids``: doc_id ->
    number of remaining attempts to fail with a retryable status before
    succeeding (simulates transient 503s).
    """

    def __init__(self, *, reachable: bool = True, fail_ids: dict[str, int] | None = None) -> None:
        self.store: dict[str, dict[str, dict[str, Any]]] = {}
        self._mappings: dict[str, dict[str, Any]] = {}
        self.indices = _Indices(self.store, self._mappings)
        self.cluster = _Cluster(healthy=reachable)
        self.reachable = reachable
        self.fail_ids = dict(fail_ids or {})
        self.bulk_calls: list[list[dict[str, Any]]] = []

    def info(self) -> dict[str, Any]:
        if not self.reachable:
            raise ConnectionError("cluster unreachable")
        return {
            "version": {"distribution": "opensearch", "number": "2.11.0"},
            "cluster_name": "fake",
        }

    def get(self, index: str, id: str) -> dict[str, Any]:  # noqa: A002
        doc = self.store.get(index, {}).get(id)
        if doc is None:
            raise KeyError(f"not found: {index}/{id}")
        return {"_index": index, "_id": id, "_source": doc, "found": True}

    def exists(self, index: str, id: str) -> bool:  # noqa: A002
        return id in self.store.get(index, {})

    def index(self, index: str, id: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: A002
        self.store.setdefault(index, {})[id] = copy.deepcopy(body)
        return {"_index": index, "_id": id, "result": "created"}

    def delete(self, index: str, id: str, ignore: list[int] | None = None) -> dict[str, Any]:  # noqa: A002
        self.store.get(index, {}).pop(id, None)
        return {"result": "deleted"}

    def delete_by_query(
        self, index: str, body: dict[str, Any], refresh: bool = False, conflicts: str = "abort"
    ) -> dict[str, Any]:
        docs = self.store.get(index, {})
        matches = [doc_id for doc_id, source in docs.items() if _matches(body["query"], source)]
        for doc_id in matches:
            docs.pop(doc_id, None)
        return {"deleted": len(matches)}

    def count(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        docs = self.store.get(index, {})
        matched = sum(1 for source in docs.values() if _matches(body["query"], source))
        return {"count": matched}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        docs = self.store.get(index, {})
        query = body.get("query", {"match_all": {}})
        size = body.get("size", 10)
        matched = [
            {"_index": index, "_id": doc_id, "_score": 1.0, "_source": source}
            for doc_id, source in docs.items()
            if _matches(query, source)
        ]
        return {"hits": {"hits": matched[:size], "total": {"value": len(matched)}}}

    def bulk(self, body: list[dict[str, Any]]) -> dict[str, Any]:
        self.bulk_calls.append(copy.deepcopy(body))
        items = []
        i = 0
        while i < len(body):
            meta = body[i]
            op = next(iter(meta))
            action_meta = meta[op]
            index = action_meta["_index"]
            doc_id = action_meta["_id"]
            if op == "delete":
                payload = None
                i += 1
            else:
                payload = body[i + 1]
                i += 2

            remaining_fail = self.fail_ids.get(doc_id, 0)
            if remaining_fail > 0:
                self.fail_ids[doc_id] = remaining_fail - 1
                error = {"type": "throttled", "reason": "simulated transient failure"}
                items.append(
                    {op: {"_index": index, "_id": doc_id, "status": 503, "error": error}}
                )
                continue

            if op == "delete":
                self.store.get(index, {}).pop(doc_id, None)
            elif op == "update":
                assert payload is not None
                doc = payload.get("doc", {})
                existing = self.store.setdefault(index, {}).setdefault(doc_id, {})
                existing.update(doc)
            else:
                assert payload is not None
                self.store.setdefault(index, {})[doc_id] = copy.deepcopy(payload)
            status = 200 if op != "index" else 201
            items.append({op: {"_index": index, "_id": doc_id, "status": status}})
        has_errors = any("error" in item.get(next(iter(item)), {}) for item in items)
        return {"errors": has_errors, "items": items}

    def close(self) -> None:
        pass


def _matches(query: dict[str, Any], source: dict[str, Any]) -> bool:
    if "match_all" in query:
        return True
    if "term" in query:
        (field, value), = query["term"].items()
        return source.get(field) == value
    if "terms" in query:
        (field, values), = query["terms"].items()
        return source.get(field) in values
    if "exists" in query:
        return query["exists"]["field"] in source
    if "bool" in query:
        clause = query["bool"]
        filters = clause.get("filter", [])
        musts = clause.get("must", [])
        shoulds = clause.get("should", [])
        must_nots = clause.get("must_not", [])
        minimum_should_match = clause.get("minimum_should_match", 1 if shoulds else 0)
        if not all(_matches(f, source) for f in filters):
            return False
        if any(_matches(mn, source) for mn in must_nots):
            return False
        for m in musts:
            if "multi_match" in m:
                q = m["multi_match"]["query"]
                fields = m["multi_match"]["fields"]
                if not any(q in str(source.get(f.split("^")[0], "")) for f in fields):
                    return False
            elif "knn" in m:
                if "embedding" not in source:
                    return False
            else:
                if not _matches(m, source):
                    return False
        if shoulds:
            matched_should = sum(1 for s in shoulds if _matches(s, source))
            if matched_should < minimum_should_match:
                return False
        return True
    return False
