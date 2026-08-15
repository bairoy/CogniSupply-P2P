import sqlite3
import sys

sys.path.append('.')
from backend.services.procurement_api.main import _catalogue
from backend.shared.db import get_conn

try:
    with get_conn() as conn:
        with conn.cursor() as cur:
            m, l = _catalogue(cur)
            print("Materials:", len(m))
            print("Locations:", len(l))
except Exception as e:
    import traceback
    traceback.print_exc()
