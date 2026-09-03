from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from pydantic import BaseModel
from sqlmodel import Session, select, func
from services.ledger_service.models import ExpenseEntry, Category, CategoryNature

class CategoryTotal(BaseModel):
    category_id: Optional[int] = None
    category_name: str
    amount: float

def get_total_spend(db: Session, session_id: str, start_date: datetime, end_date: datetime) -> float:
    """Fetch sum of spending over a given timeframe."""
    stmt = (
        select(func.sum(ExpenseEntry.amount))
        .where(ExpenseEntry.session_id == session_id)
        .where(ExpenseEntry.occurred_at >= start_date)
        .where(ExpenseEntry.occurred_at <= end_date)
    )
    res = db.exec(stmt).first()
    return float(res) if res is not None else 0.0

def get_spend_by_category(db: Session, session_id: str, start_date: datetime, end_date: datetime) -> List[CategoryTotal]:
    """Fetch spending grouped by category."""
    stmt = (
        select(Category.id, Category.name, func.sum(ExpenseEntry.amount))
        .join(Category, ExpenseEntry.category_id == Category.id)
        .where(ExpenseEntry.session_id == session_id)
        .where(ExpenseEntry.occurred_at >= start_date)
        .where(ExpenseEntry.occurred_at <= end_date)
        .group_by(Category.id, Category.name)
    )
    results = db.exec(stmt).all()
    return [
        CategoryTotal(category_id=row[0], category_name=row[1], amount=float(row[2]))
        for row in results
    ]

def get_spend_by_nature(db: Session, session_id: str, start_date: datetime, end_date: datetime) -> Dict[CategoryNature, float]:
    """Fetch spending grouped by Nature (NEED, MUST, WANT)."""
    stmt = (
        select(ExpenseEntry.nature, func.sum(ExpenseEntry.amount))
        .where(ExpenseEntry.session_id == session_id)
        .where(ExpenseEntry.occurred_at >= start_date)
        .where(ExpenseEntry.occurred_at <= end_date)
        .group_by(ExpenseEntry.nature)
    )
    results = db.exec(stmt).all()
    totals = {nature: 0.0 for nature in CategoryNature}
    for nature, amount in results:
        totals[nature] = float(amount) if amount is not None else 0.0
    return totals

def get_top_category(db: Session, session_id: str, start_date: datetime, end_date: datetime) -> Optional[CategoryTotal]:
    """Fetch the category with the highest spending."""
    stmt = (
        select(Category.id, Category.name, func.sum(ExpenseEntry.amount))
        .join(Category, ExpenseEntry.category_id == Category.id)
        .where(ExpenseEntry.session_id == session_id)
        .where(ExpenseEntry.occurred_at >= start_date)
        .where(ExpenseEntry.occurred_at <= end_date)
        .group_by(Category.id, Category.name)
        .order_by(func.sum(ExpenseEntry.amount).desc())
        .limit(1)
    )
    row = db.exec(stmt).first()
    if row:
        return CategoryTotal(category_id=row[0], category_name=row[1], amount=float(row[2]))
    return None

# Convenience range helpers
def get_this_week_range() -> Tuple[datetime, datetime]:
    """Retrieve date range for the last 7 days (or current week)."""
    now = datetime.utcnow()
    # Using 7 days ago to ensure voice prompts always return a dynamic week of data
    start = now - timedelta(days=7)
    return start, now

def get_this_month_range() -> Tuple[datetime, datetime]:
    """Retrieve date range from 1st of the current month to now."""
    now = datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now

def parse_period_range(period: str) -> Tuple[datetime, datetime]:
    """Helper to convert period string into date ranges."""
    if period == "month":
        return get_this_month_range()
    # Default to week
    return get_this_week_range()
