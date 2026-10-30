"""Run migrations against the database configured by the Flask application."""
from alembic import context
from flask import current_app

database = current_app.extensions["migrate"].db
target_metadata = database.metadata

if context.is_offline_mode():
    context.configure(url=database.engine.url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with database.engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
