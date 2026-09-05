"""pytest — додаємо parent dir у sys.path, щоб імпортувати training-модулі."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
