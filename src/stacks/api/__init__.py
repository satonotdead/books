from flask import Blueprint

api_bp = Blueprint("api", __name__)

def register_api(app):
    # Import all modules that attach routes to api_bp
    from . import annas_proxy, views, status, queue, config, history, keys
    app.register_blueprint(api_bp)
