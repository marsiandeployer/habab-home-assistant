"""Temporary local server for browser QA of the compiled demo and real DB API."""

import os
from pathlib import Path

from flask import Flask, abort, make_response, request, send_from_directory

from habab_demo.routes import ha_demo_bp

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'public'
app = Flask(__name__, static_folder=None)
app.secret_key = 'ha-demo-local-qa-only'
app.config['HA_DEMO_DATABASE_DSN'] = 'dbname=habab_home'
app.register_blueprint(ha_demo_bp)


@app.get('/qa-parent/')
def qa_parent():
    response = make_response('''<!doctype html><meta charset="utf-8"><title>HA demo parent QA</title>
      <p id="interaction-count">0</p>
      <iframe id="ha-demo" title="Демо Home Assistant" src="/demos/home-assistant/"
        style="width:100%;height:90vh;border:0"></iframe>
      <script>
        window.haDemoInteractions = 0;
        window.addEventListener('message', (event) => {
          const frame = document.getElementById('ha-demo');
          if (event.origin === location.origin && event.source === frame.contentWindow &&
              event.data?.type === 'habab-ha-demo-interaction') {
            window.haDemoInteractions += 1;
            document.getElementById('interaction-count').textContent = String(window.haDemoInteractions);
          }
        });
      </script>''')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.after_request
def add_demo_csp(response):
    if request.path.startswith('/demos/home-assistant/'):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
            "connect-src 'self'; worker-src 'self' blob:; media-src 'self' blob: data:; "
            "manifest-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self' http://127.0.0.1:5137"
        )
    return response


@app.get('/demos/home-assistant/', defaults={'asset': 'index.html'})
@app.get('/demos/home-assistant/<path:asset>')
def demo_asset(asset):
    if not (DEMO / asset).is_file():
        abort(404)
    return send_from_directory(DEMO, asset)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('HA_DEMO_QA_PORT', '5137')), threaded=True)
