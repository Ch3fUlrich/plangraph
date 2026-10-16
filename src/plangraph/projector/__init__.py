"""Projector: Temporal workflow history -> keeper projection (docs/projector.md).

This package root imports nothing (no ``temporalio``, no keeper) so that
``plangraph.keeper.schema_pg`` and ``plangraph.keeper.db`` can load the DDL in
``projector.ddl`` without an import cycle or an optional dependency.
"""
