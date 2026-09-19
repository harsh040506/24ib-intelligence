"""Flask extension singletons.

Instantiated here (unbound) and initialised against the app in the factory.
Keeping them in one module avoids circular imports between models and routes.
"""
from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
