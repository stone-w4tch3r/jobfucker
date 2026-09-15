"""Storage layer: SQLAlchemy ORM models + repositories over frozen domain DTOs.

SQLAlchemy is confined to this package (ruff ``banned-api``). The public surface
for engine / CLI / UI is:
- :data:`dto.Pipeline`, :data:`dto.VacancyRecord`, :data:`dto.AuditLogEntry`,
  :data:`dto.DailyLimit` — frozen, ORM-free, board-neutral value objects;
- :func:`db.open_storage` (file DB), :func:`db.storage_from_engine` (tests) and
  the repository objects they expose;
- :func:`db.create_schema` / the Alembic migrations for schema creation.
"""
