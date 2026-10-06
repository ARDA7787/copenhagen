"""A second review is an authenticated decision over immutable content, not a name."""

from copenhagen.audit.chain import append
from copenhagen.core.canonical import sha256_hex
from copenhagen.core.capability import CapabilitySpec
from copenhagen.db.models import PublicationReview
from copenhagen.db.store import get, lock, new_id, put, transaction
from copenhagen.registry.publish import publish
from copenhagen.service import Service


def propose(service: Service, actor: str, spec: CapabilitySpec) -> str:
    with transaction(service.engine) as session:
        identity = service.identity(session, actor)
        if not {"admin", "capability_author"} & set(identity.roles):
            raise ValueError("publishing authority required")
        data = spec.model_dump(mode="json", by_alias=True)
        id_ = new_id("review")
        put(
            session,
            PublicationReview,
            service.tenant,
            id_,
            {
                "spec": data,
                "spec_hash": sha256_hex(data),
                "author": actor,
            },
            "pending",
        )
        append(
            session,
            service.tenant,
            id_,
            "capability.review_requested",
            principal_id=actor,
            capability=spec.ref,
            spec_hash=sha256_hex(data),
        )
        return id_


def decide(service: Service, actor: str, id_: str, approved: bool, reason: str) -> None:
    if not reason.strip():
        raise ValueError("review requires a reason")
    with transaction(service.engine) as session:
        lock(session, f"review:{service.tenant}:{id_}")
        identity = service.identity(session, actor)
        item = get(session, PublicationReview, service.tenant, id_)
        author = service.identity(session, item.data["author"])
        if actor == author.id:
            raise ValueError("independent reviewer required")
        if any(
            p.status != "active" or not {"admin", "capability_author"} & set(p.roles)
            for p in (identity, author)
        ):
            raise ValueError("active publishing authority required for both reviewers")
        if item.status != "pending":
            raise ValueError("review already decided")
        if sha256_hex(item.data["spec"]) != item.data["spec_hash"]:
            raise ValueError("reviewed content changed")
        spec = CapabilitySpec.model_validate(item.data["spec"])
        if approved:
            publish(session, service.tenant, spec, author.id, actor)
        item.status = "approved" if approved else "rejected"
        item.data = {**item.data, "reviewer": actor, "reason": reason}
        append(
            session,
            service.tenant,
            id_ + ":decided",
            "capability.review_decided",
            principal_id=actor,
            capability=spec.ref,
            approved=approved,
            spec_hash=item.data["spec_hash"],
            reason=reason,
        )
