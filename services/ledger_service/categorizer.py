import re
from typing import Optional, List, Tuple
from services.ledger_service.models import Category, CategoryNature

# Define keyword mapping rules mapping keywords in payee/labels to Category Names
KEYWORD_MAPPING = {
    "Groceries": [
        "groceries", "grocery", "supermarket", "mart", "milk", "veg", "vegetable",
        "kirana", "basket", "blinkit", "instamart", "bigbasket", "zepto"
    ],
    "Dining Out": [
        "dining", "restaurant", "food", "swiggy", "zomato", "cafe", "pizza",
        "burger", "starbucks", "mcdonald", "kfc", "eats", "dhaba", "bar"
    ],
    "Utilities": [
        "utilities", "utility", "electricity", "water", "gas", "bill", "recharge",
        "internet", "wifi", "broadband", "mobile", "phone", "bsnl", "jio", "airtel"
    ],
    "Transport": [
        "transport", "travel", "uber", "ola", "rapido", "metro", "auto", "train",
        "bus", "flight", "taxi", "cab", "irctc", "fuel", "petrol"
    ],
    "Entertainment": [
        "entertainment", "netflix", "movie", "show", "theatre", "spotify",
        "youtube", "prime", "hotstar", "game", "gaming", "steam", "concert"
    ],
    "Rent": [
        "rent", "landlord", "apartment", "pg", "flat", "housing", "rentomojo"
    ]
}

def categorize(
    payee: Optional[str],
    label: Optional[str],
    categories: List[Category]
) -> Tuple[Optional[int], CategoryNature]:
    """
    Deterministic rule-based classifier for mapping payee names and transcripts to a category.
    
    TODO: Extension Seam
    To swap in a more advanced LLM-based classifier later (e.g. calling Gemini API
    to classify unstructured voice transcripts), replace this function's body
    with a prompt call to Gemini.
    """
    # 1. Normalize input text
    search_texts = []
    if payee:
        search_texts.append(payee.lower())
    if label:
        search_texts.append(label.lower())
    
    combined = " ".join(search_texts)
    
    matched_name = None
    
    # 2. Check keyword maps
    for cat_name, keywords in KEYWORD_MAPPING.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', combined):
                matched_name = cat_name
                break
        if matched_name:
            break
            
    if not matched_name:
        matched_name = "Other"
        
    # 3. Find Category in database list
    category_map = {c.name.lower(): c for c in categories}
    matched_category = category_map.get(matched_name.lower())
    
    if matched_category:
        return matched_category.id, matched_category.nature
    else:
        # Fallback if DB doesn't have the categories yet
        return None, CategoryNature.NEED
