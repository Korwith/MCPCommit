"""Where OmniMCP keeps packs, caches and workspaces."""

import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # backend/
DATA_DIR = os.path.abspath(os.environ.get('MCPWB_DATA') or os.environ.get('MCPSTUDIO_DATA') or
                           os.path.join(os.path.dirname(APP_DIR), 'data'))
WORKSPACES_DIR = os.path.join(DATA_DIR, 'workspaces')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')

for _d in (DATA_DIR, WORKSPACES_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)
