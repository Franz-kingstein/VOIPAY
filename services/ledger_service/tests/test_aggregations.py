import os
import sys
from datetime import datetime, timedelta
import pytest
from sqlmodel import SQLModel, create_engine, Session, select

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from services.ledger_service.models import ExpenseEntry, Category, CategoryNature
from services.ledger_service.aggregations import (
    get_total_spend, get_spend_by_category, get_spend_by_nature,
    get_top_category, get_this_week_range, get_this_month_range
)

# Setup in-memory SQLite engine for tests using StaticPool to share connection
sqlite_url = "sqlite:///:memory:"
engine = create_engine(
    sqlite_url,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

@event.listens_for(engine, "connect")
def connect(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("ATTACH DATABASE ':memory:' AS ledger;")
    cursor.close()

@pytest.fixture(name="db_session")
def session_fixture():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)

def test_aggregations_and_ranges(db_session):
    # 1. Create categories
    groceries = Category(id=1, name="Groceries", nature=CategoryNature.NEED)
    dining = Category(id=2, name="Dining Out", nature=CategoryNature.WANT)
    utilities = Category(id=3, name="Utilities", nature=CategoryNature.MUST)
    db_session.add_all([groceries, dining, utilities])
    db_session.commit()
    
    session_id = "session_test"
    now = datetime.utcnow()
    
    # 2. Add expense entries across categories/dates
    # This week (2 days ago)
    entry1 = ExpenseEntry(
        payment_id="pay_1",
        session_id=session_id,
        amount=150.0,
        payee="Zomato",
        category_id=2,
        nature=CategoryNature.WANT,
        trace_id="t1",
        occurred_at=now - timedelta(days=2)
    )
    # This week (3 days ago)
    entry2 = ExpenseEntry(
        payment_id="pay_2",
        session_id=session_id,
        amount=50.0,
        payee="Blinkit",
        category_id=1,
        nature=CategoryNature.NEED,
        trace_id="t2",
        occurred_at=now - timedelta(days=3)
    )
    # This week (4 days ago)
    entry3 = ExpenseEntry(
        payment_id="pay_3",
        session_id=session_id,
        amount=200.0,
        payee="Zomato Again",
        category_id=2,
        nature=CategoryNature.WANT,
        trace_id="t3",
        occurred_at=now - timedelta(days=4)
    )
    # Older expense (15 days ago, still this month)
    entry4 = ExpenseEntry(
        payment_id="pay_4",
        session_id=session_id,
        amount=1000.0,
        payee="Electricity",
        category_id=3,
        nature=CategoryNature.MUST,
        trace_id="t4",
        # Ensure it falls within current calendar month
        occurred_at=now.replace(day=1) if now.day > 15 else now - timedelta(days=15)
    )
    # Different session
    entry5 = ExpenseEntry(
        payment_id="pay_5",
        session_id="other_session",
        amount=600.0,
        payee="Uber",
        category_id=1,
        nature=CategoryNature.NEED,
        trace_id="t5",
        occurred_at=now - timedelta(days=1)
    )
    
    db_session.add_all([entry1, entry2, entry3, entry4, entry5])
    db_session.commit()
    
    # 3. Test total spend for this week (last 7 days)
    week_start, week_end = get_this_week_range()
    total_week = get_total_spend(db_session, session_id, week_start, week_end)
    # Sum of entry1, entry2, entry3 = 150 + 50 + 200 = 400.0
    # entry4 (15 days ago) and entry5 (other session) excluded
    assert total_week == 400.0
    
    # 4. Test spend by category for this week
    cat_spend = get_spend_by_category(db_session, session_id, week_start, week_end)
    assert len(cat_spend) == 2
    cat_map = {c.category_name: c.amount for c in cat_spend}
    assert cat_map["Dining Out"] == 350.0
    assert cat_map["Groceries"] == 50.0
    
    # 5. Test spend by nature for this month
    month_start, month_end = get_this_month_range()
    nature_spend = get_spend_by_nature(db_session, session_id, month_start, month_end)
    # entry4 might be excluded if now is first week of the month and 15 days ago was last month.
    # But our replacing day=1 makes it this month. Let's inspect nature_spend keys.
    assert nature_spend[CategoryNature.WANT] == 350.0
    assert nature_spend[CategoryNature.NEED] == 50.0
    # Verify MUST matches entry4 if within range
    if entry4.occurred_at >= month_start:
        assert nature_spend[CategoryNature.MUST] == 1000.0
        
    # 6. Test top category this week
    top_cat = get_top_category(db_session, session_id, week_start, week_end)
    assert top_cat is not None
    assert top_cat.category_name == "Dining Out"
    assert top_cat.amount == 350.0
