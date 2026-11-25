"""Local durable submission fence before formal teaching-portal POSTs."""
from __future__ import annotations

import secrets

from .. import catalog_snapshots as catalog
from .assignment_workflow import PreparationError, _requirements, _verified_saved_receipt


class SubmissionPermit:
    """Durable local state machine guarding single-attempt assignment uploads."""

    def __init__(self, settings, metadata, assignment, save):
        self.settings = settings
        self.metadata = metadata
        self.assignment = assignment
        self.save = save
        self.armed = False
        self.recovered_receipt = None

    def reconcile(self):
        """Recover verified receipts or retire an unarmed local submission draft."""
        identity = self.metadata.get("submission_fence")
        if not isinstance(identity, dict) or identity.get("protocol") != 1:
            raise PreparationError("旧提交记录没有可验证的本地状态，请到教学网核对；禁止自动解除。")
        state = identity.get("state") or "acquired"
        if state in ("verification_pending", "submitted"):
            if (
                self.metadata.get("state") not in ("receipt_verified", "submitted")
                or not _verified_saved_receipt({**self.metadata, "state": "submitted"})
            ):
                raise PreparationError("提交可能已发出，但没有本次完整回执；请到教学网核对，切勿重复发送。")
            self.complete(self.metadata["receipt"])
            self.metadata["state"] = "submitted"
            self.save()
            return {"state": "submitted", "message": "已恢复回执保存，未重新发送作业。"}
        if state not in ("acquired", "aborted"):
            raise PreparationError("提交记录状态未确认，禁止自动解除。")
        identity["state"] = "aborted"
        self.metadata["state"] = "aborted"
        self.save()
        return {"state": "aborted", "message": "本地已确认该请求未进入发送阶段，可重新准备并预览新草稿。"}

    def __enter__(self):
        if not self.metadata.get("submission_fence"):
            try:
                current = catalog.current(self.settings, self.metadata["course_id"])
            except catalog.CatalogUnavailable as exc:
                raise PreparationError("本地正式作业快照不可用，请先更新课程目录；未发送作业。") from exc
            matches = [
                item
                for item in current["snapshot"]["sections"]["assignments"]["items"]
                if item.get("content_id") == self.metadata["content_id"] and item.get("source") != "calendar"
            ]
            if len(matches) != 1 or _requirements(matches[0]) != self.metadata["requirements_sha256"]:
                raise PreparationError("本地最新正式作业要求与预览不一致，请更新目录并重新准备。")
            self.metadata["submission_fence"] = {
                "protocol": 1,
                "revision": current["revision"],
                "permit_id": secrets.token_hex(16),
                "state": "acquired",
            }
            self.save()

        identity = self.metadata["submission_fence"]
        if not identity.get("permit_id"):
            identity["permit_id"] = secrets.token_hex(16)
            self.save()
        state = identity.get("state") or "acquired"
        if state in ("verification_pending", "submitted"):
            self.armed = True
            if (
                self.metadata.get("state") not in ("receipt_verified", "submitted")
                or not _verified_saved_receipt({**self.metadata, "state": "submitted"})
            ):
                raise PreparationError("该请求已发出或尚待核验；没有本次完整回执，禁止再次提交。")
            receipt = self.metadata["receipt"]
            self.complete(receipt)
            self.recovered_receipt = receipt
        elif state != "acquired":
            raise PreparationError("这份草稿的提交状态已结束，请重新准备。")
        else:
            identity["state"] = "acquired"
            self.save()
        return self

    def arm(self):
        self.armed = True
        self.metadata["submission_fence"]["state"] = "verification_pending"
        self.save()

    def complete(self, receipt):
        if not _verified_saved_receipt({**self.metadata, "state": "submitted", "receipt": receipt}):
            raise PreparationError("本次教学网回执内容或目标未验证，保留待核验状态。")
        fields = ("course_id", "content_id", "portal_verified", "verified_at", "review_history_sha256")
        proof = {key: receipt[key] for key in fields}
        self.metadata["submission_fence"]["receipt"] = proof
        self.metadata["submission_fence"]["state"] = "submitted"
        self.save()

    def __exit__(self, *_):
        if not self.armed and isinstance(self.metadata.get("submission_fence"), dict):
            self.metadata["submission_fence"]["state"] = "aborted"
            self.save()
