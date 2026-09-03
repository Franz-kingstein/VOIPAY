from datetime import datetime
from enum import Enum
from typing import Optional
from sqlmodel import SQLModel, Field

class CategoryNature(str, Enum):
    WANT = "want"
    NEED = "need"
    MUST = "must"

class Category(SQLModel, table=True):
    __tablename__ = "categories"
    __table_args__ = {"schema": "ledger"}
    
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    nature: CategoryNature
    parent_category_id: Optional[int] = Field(default=None, foreign_key="ledger.categories.id")
    color: Optional[str] = None  # hex format, e.g. #FF5733

class ExpenseEntry(SQLModel, table=True):
    __tablename__ = "expense_entries"
    __table_args__ = {"schema": "ledger"}
    
    id: Optional[int] = Field(default=None, primary_key=True)
    payment_id: str = Field(index=True, unique=True)  # Links to captured payment
    session_id: str = Field(index=True)
    amount: float
    payee: Optional[str] = None
    category_id: Optional[int] = Field(default=None, foreign_key="ledger.categories.id")
    nature: CategoryNature
    label: Optional[str] = None  # Voice prompt text context / description
    trace_id: str
    occurred_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
