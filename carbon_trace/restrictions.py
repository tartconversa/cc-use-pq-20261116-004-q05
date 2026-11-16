from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import Conflict, Invariant, Stale, require
from .quality import Quality
from .store import Store
from .values import identifier, instant, text


class Restrictions:
    """来源限制：带生效时间与解除记录，沿材料身份（lots.source）传播。

    限制是追加式证据：解除只写 lifted_at/lifted_revision，不删除任何记录；
    历史发运、检验与已发布报表均不受影响。
    """

    def __init__(self, store: Store):
        self.store = store
        self.quality = Quality()

    def open(self, payload: dict) -> dict:
        restriction_id = identifier(payload.get("restriction_id"), "restriction_id")
        source = identifier(payload.get("source"), "source")
        reason = text(payload.get("reason"), "reason", max_length=400)
        effective_at = instant(payload.get("effective_at"), "effective_at")
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM source_restrictions WHERE restriction_id=?",
                          (restriction_id,)).fetchone():
                raise Conflict("restriction_id already exists")
            open_row = db.execute(
                "SELECT 1 FROM source_restrictions WHERE source=? AND lifted_at IS NULL",
                (source,)).fetchone()
            if open_row:
                raise Invariant("source already has an active restriction")
            revision = Store.advance(db, "restriction_open", restriction_id)
            db.execute("INSERT INTO source_restrictions VALUES (?,?,?,?,?,?,?)",
                       (restriction_id, source, reason, effective_at, None, revision, None))
            return {"restriction_id": restriction_id, "source": source, "reason": reason,
                    "effective_at": effective_at, "lifted_at": None,
                    "created_revision": revision, "lifted_revision": None,
                    "status": "active", "currently_active": self._not_after(effective_at, self._now())}

    def lift(self, restriction_id: str, payload: dict) -> dict:
        restriction_id = identifier(restriction_id, "restriction_id")
        payload = payload if isinstance(payload, dict) else {}
        lifted_at = instant(payload.get("lifted_at"), "lifted_at")
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0, "invalid expected_revision")
        with self.store.write() as db:
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed before lift")
            row = Store.one(db, "SELECT * FROM source_restrictions WHERE restriction_id=?",
                            (restriction_id,), "restriction")
            if row["lifted_at"] is not None:
                raise Conflict("restriction already lifted")
            require(self._not_after(row["effective_at"], lifted_at), "lift precedes effective time")
            revision = Store.advance(db, "restriction_lift", restriction_id)
            db.execute("UPDATE source_restrictions SET lifted_at=?,lifted_revision=? WHERE restriction_id=?",
                       (lifted_at, revision, restriction_id))
            return {"restriction_id": restriction_id, "source": row["source"],
                    "reason": row["reason"], "effective_at": row["effective_at"],
                    "lifted_at": lifted_at, "created_revision": row["created_revision"],
                    "lifted_revision": revision, "status": "lifted",
                    "currently_active": False}

    def get(self, restriction_id: str) -> dict:
        with self.store.read() as db:
            return self._present(Store.one(
                db, "SELECT * FROM source_restrictions WHERE restriction_id=?",
                (identifier(restriction_id, "restriction_id"),), "restriction"))

    def list_for_source(self, source: str) -> list[dict]:
        source = identifier(source, "source")
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM source_restrictions WHERE source=? ORDER BY effective_at,restriction_id",
                              (source,)).fetchall()
            return [self._present(row) for row in rows]

    def list_all(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM source_restrictions ORDER BY effective_at,restriction_id").fetchall()
            return [self._present(row) for row in rows]

    # ---- 时点语义与传播 -------------------------------------------------

    @staticmethod
    def _now() -> str:
        # 规整到秒精度，与 instant() 归一化后的存储格式保持一致，SQL/字符串比较方可安全
        return (datetime.now(timezone.utc).replace(microsecond=0)
                .isoformat().replace("+00:00", "Z"))

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @classmethod
    def _not_after(cls, left: str, right: str) -> bool:
        """left <= right，基于真实时间而非字符串字典序（微秒精度差异安全）。"""
        return cls._parse(left) <= cls._parse(right)

    @classmethod
    def _after(cls, left: str, right: str) -> bool:
        return cls._parse(left) > cls._parse(right)

    @staticmethod
    def day_end_instant(target_day: str, zone: str) -> str:
        end_local = datetime.fromisoformat(target_day) + timedelta(days=1)
        return end_local.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)\
            .isoformat().replace("+00:00", "Z")

    @classmethod
    def active_rows(cls, db, source: str, at: str) -> list:
        """在 at（UTC instant）时点对该来源生效中的限制记录。"""
        return db.execute(
            "SELECT * FROM source_restrictions WHERE source=? AND effective_at<=? "
            "AND (lifted_at IS NULL OR lifted_at>?) ORDER BY effective_at,restriction_id",
            (source, at, at)).fetchall()

    @staticmethod
    def summary(rows: list) -> dict | None:
        if not rows:
            return None
        return {"restriction_ids": [row["restriction_id"] for row in rows],
                "reasons": [row["reason"] for row in rows],
                "effective_at": min(row["effective_at"] for row in rows),
                "lifted_at": None}

    @classmethod
    def ensure_commitment_allowed(cls, db, lot) -> None:
        """预约硬阻断：受限制的材料身份不得用于新的客户承诺。"""
        rows = cls.active_rows(db, lot["source"], cls._now())
        if rows:
            raise Invariant("lot source is restricted for new commitments",
                            detail={"lot_id": lot["lot_id"], "source": lot["source"],
                                    "restriction_ids": [row["restriction_id"] for row in rows]})

    def explain(self, lot_id: str, at: str | None = None) -> dict:
        """说明某批材料在特定时间为什么可用或不可用（质量 + 来源限制）。"""
        lot_id = identifier(lot_id, "lot_id")
        with self.store.read() as db:
            lot = Store.one(db, "SELECT lots.*,plants.timezone FROM lots JOIN lines "
                            "ON lines.line_id=lots.line_id JOIN plants ON plants.plant_id=lines.plant_id "
                            "WHERE lots.lot_id=?", (lot_id,), "lot")
            at = instant(at, "at") if at is not None else self._now()
            quality = self.quality.classify(db, lot, cutoff=at)
            restriction_rows = self.active_rows(db, lot["source"], at)
            restricted = bool(restriction_rows)
            availability_reasons = []
            commitment_reasons = []
            if restricted:
                availability_reasons.append("source_restricted")
                commitment_reasons.append("source_restricted")
            if quality["status"] == "unmeasured":
                commitment_reasons.append("quality_unmeasured")
            elif quality["status"] == "failed":
                commitment_reasons.append("quality_failed")
            available = not restricted
            commitment_allowed = available and quality["status"] == "passed"
            return {"lot_id": lot_id, "source": lot["source"], "at": at,
                    "quality_status": quality["status"], "quality": quality,
                    "restricted": restricted,
                    "restrictions": [self._present(row) for row in restriction_rows],
                    "available": available,
                    "availability_reasons": availability_reasons,
                    "commitment_allowed": commitment_allowed,
                    "commitment_reasons": commitment_reasons,
                    "source_revision": Store.revision(db)}

    # ---- 既有承诺的人工复核 ---------------------------------------------

    def _legacy_rows(self, db, restriction_id: str) -> list:
        """限制解除前已经存在、且仍 active 的预约（既存承诺）。"""
        restriction = Store.one(db, "SELECT * FROM source_restrictions WHERE restriction_id=?",
                                (restriction_id,), "restriction")
        require(restriction["lifted_at"] is not None, "restriction is not lifted yet")
        sql = ("SELECT reservations.*,lots.source FROM reservations JOIN lots "
               "ON lots.lot_id=reservations.lot_id WHERE lots.source=? "
               "AND reservations.status='active' AND reservations.created_revision<=? "
               "AND NOT EXISTS (SELECT 1 FROM restriction_reviews rv WHERE "
               "rv.restriction_id=? AND rv.reservation_id=reservations.reservation_id) "
               "ORDER BY reservations.created_revision,reservations.reservation_id")
        return db.execute(sql, (restriction["source"], restriction["lifted_revision"],
                                restriction_id)).fetchall()

    def review_queue(self, restriction_id: str) -> dict:
        restriction_id = identifier(restriction_id, "restriction_id")
        with self.store.read() as db:
            restriction = Store.one(db, "SELECT * FROM source_restrictions WHERE restriction_id=?",
                                    (restriction_id,), "restriction")
            rows = self._legacy_rows(db, restriction_id)
            pending = [{"reservation_id": row["reservation_id"], "order_id": row["order_id"],
                        "lot_id": row["lot_id"], "quantity": row["quantity"],
                        "created_revision": row["created_revision"]} for row in rows]
            return {"restriction_id": restriction_id, "source": restriction["source"],
                    "status": restriction["lifted_at"] and "lifted" or "active",
                    "lifted_at": restriction["lifted_at"],
                    "pending_count": len(pending), "pending": pending,
                    "source_revision": Store.revision(db)}

    def review(self, payload: dict) -> dict:
        restriction_id = identifier(payload.get("restriction_id"), "restriction_id")
        reservation_id = identifier(payload.get("reservation_id"), "reservation_id")
        note = text(payload.get("note"), "note", max_length=400)
        with self.store.write() as db:
            restriction = Store.one(db, "SELECT * FROM source_restrictions WHERE restriction_id=?",
                                    (restriction_id,), "restriction")
            require(restriction["lifted_at"] is not None, "restriction is not lifted yet")
            reservation = Store.one(db, "SELECT * FROM reservations WHERE reservation_id=?",
                                    (reservation_id,), "reservation")
            lot = Store.one(db, "SELECT * FROM lots WHERE lot_id=?", (reservation["lot_id"],), "lot")
            if lot["source"] != restriction["source"]:
                raise Invariant("reservation does not belong to restricted source")
            if reservation["status"] != "active":
                raise Invariant("reservation is not active")
            if reservation["created_revision"] > restriction["lifted_revision"]:
                raise Invariant("reservation was created after the restriction was lifted")
            if db.execute("SELECT 1 FROM restriction_reviews WHERE restriction_id=? AND reservation_id=?",
                          (restriction_id, reservation_id)).fetchone():
                raise Conflict("reservation already reviewed for this restriction")
            revision = Store.advance(db, "restriction_review", f"{restriction_id}:{reservation_id}")
            db.execute("INSERT INTO restriction_reviews VALUES (?,?,?,?)",
                       (restriction_id, reservation_id, note, revision))
            return {"restriction_id": restriction_id, "reservation_id": reservation_id,
                    "note": note, "reviewed_revision": revision}

    def pending_legacy_reservation(self, db, source: str, reservation_id: str) -> bool:
        """该预约是否是某条已解除限制下尚未人工复核的既存承诺。"""
        row = db.execute(
            "SELECT r.created_revision AS created_revision FROM reservations r "
            "WHERE r.reservation_id=? AND r.status='active'", (reservation_id,)).fetchone()
        if row is None:
            return False
        match = db.execute(
            "SELECT sr.restriction_id FROM source_restrictions sr WHERE sr.source=? "
            "AND sr.lifted_at IS NOT NULL AND sr.lifted_revision>=? "
            "AND NOT EXISTS (SELECT 1 FROM restriction_reviews rv WHERE rv.restriction_id=sr.restriction_id "
            "AND rv.reservation_id=?)",
            (source, row["created_revision"], reservation_id)).fetchone()
        return match is not None

    # ---- 展示辅助 -------------------------------------------------------

    def _present(self, row) -> dict:
        now = self._now()
        currently_active = (self._not_after(row["effective_at"], now) and
                            (row["lifted_at"] is None or self._after(row["lifted_at"], now)))
        return {"restriction_id": row["restriction_id"], "source": row["source"],
                "reason": row["reason"], "effective_at": row["effective_at"],
                "lifted_at": row["lifted_at"], "created_revision": row["created_revision"],
                "lifted_revision": row["lifted_revision"],
                "status": "lifted" if row["lifted_at"] is not None else "active",
                "currently_active": currently_active}