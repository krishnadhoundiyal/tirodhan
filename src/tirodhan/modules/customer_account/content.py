from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import utc_now
from tirodhan.modules.customer_account.models import ContentParagraph, ContentRevision
from tirodhan.modules.customer_account.schemas import (
    ContentDraft,
    ContentDto,
    ContentPublish,
    ContentRevisionDto,
    Slug,
)
from tirodhan.modules.customer_reads.errors import CustomerReadError


async def read_content(session: AsyncSession, slug: Slug) -> ContentDto:
    row = await session.scalar(
        select(ContentRevision).where(
            ContentRevision.slug == slug, ContentRevision.status == "PUBLISHED"
        )
    )
    if row is None:
        raise CustomerReadError(404, "CONTENT_UNPUBLISHED")
    assert row.published_at is not None
    paragraphs = list(
        await session.scalars(
            select(ContentParagraph.body)
            .where(ContentParagraph.revision_id == row.revision_id)
            .order_by(ContentParagraph.position)
        )
    )
    return ContentDto(
        slug=slug,
        title=row.title,
        paragraphs=paragraphs,
        updated_at=row.published_at,
        revision=row.revision,
    )


async def change_content(
    session: AsyncSession,
    *,
    actor: UUID,
    slug: Slug,
    body: ContentDraft | ContentPublish,
    revision_id: UUID | None = None,
) -> ContentRevisionDto:
    # One conventional publication stream per allow-listed document, including its first draft.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"content:{slug}"}
    )
    version = (
        await session.scalar(
            select(func.max(ContentRevision.revision)).where(ContentRevision.slug == slug)
        )
        or 0
    )
    if body.expected_version != version:
        raise CustomerReadError(409, "VERSION_CONFLICT")
    if isinstance(body, ContentDraft):
        row = ContentRevision(
            slug=slug, revision=version + 1, title=body.title, authored_by=actor, status="DRAFT"
        )
        session.add(row)
        await session.flush()
        session.add_all(
            [
                ContentParagraph(revision_id=row.revision_id, position=i, body=p)
                for i, p in enumerate(body.paragraphs)
            ]
        )
    else:
        found = await session.get(ContentRevision, revision_id)
        if found is None or found.slug != slug:
            raise CustomerReadError(404, "CONTENT_NOT_FOUND")
        row = found
        if row.status != "DRAFT" or row.revision != version:
            raise CustomerReadError(409, "VERSION_CONFLICT")
        previous = await session.scalar(
            select(ContentRevision)
            .where(ContentRevision.slug == slug, ContentRevision.status == "PUBLISHED")
            .with_for_update()
        )
        if previous is not None:
            previous.status = "RETIRED"
            await session.flush()
        row.status = "PUBLISHED"
        row.approved_by = actor
        row.approval_reference = body.approval_reference
        row.published_at = utc_now()
    await session.flush()
    return ContentRevisionDto(revision_id=row.revision_id, revision=row.revision, status=row.status)
