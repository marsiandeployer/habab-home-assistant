"""Standalone Habab Home demo API; no connection to a real home."""
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify

from habab_demo.routes import ha_demo_bp, HADemoSessionInterface
from habab_demo import chat  # Registers the isolated public help-chat routes.

ROOT = Path(__file__).resolve().parent


def create_app():
    load_dotenv(ROOT.parent / '.env')
    app = Flask(__name__, static_folder=str(ROOT / 'public'))
    app.secret_key = os.environ['SECRET_KEY']
    app.config['HA_DEMO_DATABASE_DSN'] = os.environ['HA_DEMO_DATABASE_DSN']
    app.session_interface = HADemoSessionInterface()
    app.register_blueprint(ha_demo_bp)

    @app.get('/health')
    def health():
        return jsonify(service='habab-home-assistant', status='ok')

    return app


application = create_app()
