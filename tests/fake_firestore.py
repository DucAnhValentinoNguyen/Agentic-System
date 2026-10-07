"""A small in-memory stand-in for the parts of the Firestore async client that FirestoreSaver uses."""

from types import SimpleNamespace


class Snap:
    def __init__(self, ref, data):
        self.reference, self.id, self._d = ref, ref.id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.id = path.rsplit("/", 1)[-1]

    def collection(self, name):
        return Coll(self.db, f"{self.path}/{name}")

    async def get(self):
        return Snap(self, self.db.docs.get(self.path))

    async def set(self, data, merge=False):
        self.db.docs[self.path] = {**(self.db.docs.get(self.path) or {}), **data} if merge else dict(data)

    async def delete(self):
        self.db.docs.pop(self.path, None)


class Coll:
    def __init__(self, db, path, filters=(), order=None, lim=None):
        self.db, self.path, self.filters, self.order, self.lim = db, path, filters, order, lim

    def document(self, name):
        return Ref(self.db, f"{self.path}/{name}")

    def where(self, field, op, value):
        assert op == "=="
        return Coll(self.db, self.path, (*self.filters, (field, value)), self.order, self.lim)

    def order_by(self, field, direction="ASCENDING"):
        assert field == "__name__"
        return Coll(self.db, self.path, self.filters, direction, self.lim)

    def limit(self, n):
        return Coll(self.db, self.path, self.filters, self.order, n)

    async def stream(self):
        prefix = self.path + "/"
        rows = [(p, d) for p, d in sorted(self.db.docs.items())
                if p.startswith(prefix) and "/" not in p[len(prefix):]
                and all(d.get(f) == v for f, v in self.filters)]
        if self.order == "DESCENDING":
            rows.reverse()
        for p, d in rows[: self.lim]:
            yield Snap(Ref(self.db, p), d)


class Batch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def set(self, ref, data, merge=False):
        self.ops.append((ref.path, data, merge))

    async def commit(self):
        for path, data, merge in self.ops:
            self.db.docs[path] = {**(self.db.docs.get(path) or {}), **data} if merge else dict(data)


class FakeDB:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return Coll(self, name)

    def batch(self):
        return Batch(self)

    async def get_all(self, refs):
        for r in refs:
            yield Snap(r, self.docs.get(r.path))


__all__ = ["FakeDB", "SimpleNamespace"]
