import sys, os; sys.path.insert(0, "."); os.chdir(".")
from app.api.history import router
print("OK", router)

