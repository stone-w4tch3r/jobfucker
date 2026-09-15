"""Clients and the frozen contract boundary.

``base.py`` holds the contract (``Client`` protocol, vendor-neutral models,
``ClientError`` taxonomy, shared seams); ``factory.py`` is the service-name →
client registry. Concrete clients (``mock/`` here; ``hh/`` in the HH track)
live in subpackages and are the only modules that import ``base.py``.
"""

from __future__ import annotations
