import argparse
import json
from pathlib import Path

import uvicorn

from mediadock.models import ROOT, Settings

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run MediaDock locally (127.0.0.1 only).')
    parser.add_argument('--home', type=Path, default=ROOT, help='Separate config/history directory')
    args = parser.parse_args()
    from mediadock.app import create_app
    app = create_app(args.home)
    port = app.state.engine.settings.port
    print(f'MediaDock: http://127.0.0.1:{port}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=port, proxy_headers=False, log_level='info')
