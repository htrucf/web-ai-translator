import sys, os; sys.path.insert(0, "."); os.chdir(".")
from app.main import app
routes = [r.path for r in app.routes]
for r in routes:
    if "history" in r:
        print(r)

