def register_blueprints(app):
    from routes.chat import bp as chat_bp
    from routes.schema import bp as schema_bp
    from routes.knowledge import bp as knowledge_bp
    from routes.meta import bp as meta_bp
    from routes.offline_exit import bp as offline_exit_bp
    for blueprint in (chat_bp, schema_bp, knowledge_bp, meta_bp, offline_exit_bp): app.register_blueprint(blueprint)
