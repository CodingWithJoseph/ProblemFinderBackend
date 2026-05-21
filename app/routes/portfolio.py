from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.models import PortfolioProduct, ProductStatusEnum
from app.auth import get_uid
from pydantic import BaseModel

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class PortfolioCreateRequest(BaseModel):
    opportunityId: int | None = None
    name: str
    description: str | None = None
    url: str | None = None
    category: str | None = None
    revenueModel: str | None = None
    notes: str | None = None


class PortfolioUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    category: str | None = None
    revenueModel: str | None = None
    status: ProductStatusEnum | None = None
    notes: str | None = None


@router.get("")
async def get_portfolio(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(PortfolioProduct).where(PortfolioProduct.uid == uid)
    )
    products = result.scalars().all()
    return {"data": [_serialize(p) for p in products]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(
    body: PortfolioCreateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = PortfolioProduct(
        uid=uid,
        opportunity_id=body.opportunityId,
        name=body.name,
        description=body.description,
        url=body.url,
        category=body.category,
        revenue_model=body.revenueModel,
        notes=body.notes,
    )
    db.add(product)
    await db.flush()
    await db.refresh(product)
    return _serialize(product)


@router.patch("/{product_id}")
async def update_product(
    product_id: int,
    body: PortfolioUpdateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _get_product(product_id, uid, db)

    if body.name is not None:
        product.name = body.name
    if body.description is not None:
        product.description = body.description
    if body.url is not None:
        product.url = body.url
    if body.category is not None:
        product.category = body.category
    if body.revenueModel is not None:
        product.revenue_model = body.revenueModel
    if body.status is not None:
        product.status = body.status
    if body.notes is not None:
        product.notes = body.notes

    return _serialize(product)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _get_product(product_id, uid, db)
    await db.delete(product)


async def _get_product(product_id: int, uid: str, db: AsyncSession) -> PortfolioProduct:
    result = await db.execute(
        select(PortfolioProduct).where(
            PortfolioProduct.id == product_id,
            PortfolioProduct.uid == uid,
        )
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _serialize(p: PortfolioProduct) -> dict:
    return {
        "id": p.id,
        "uid": p.uid,
        "opportunityId": p.opportunity_id,
        "name": p.name,
        "description": p.description,
        "url": p.url,
        "category": p.category,
        "revenueModel": p.revenue_model,
        "status": p.status,
        "notes": p.notes,
    }