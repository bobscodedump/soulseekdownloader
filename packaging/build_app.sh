#!/bin/zsh
# Build "DJ Library.app" into djlib/dist. Usage: packaging/build_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."
(cd web-src && npm run build)
uv run pyinstaller --noconfirm --clean --windowed --name "DJ Library" \
  --osx-bundle-identifier com.djlib.app \
  --add-data "src/djlib/web:djlib/web" \
  --collect-submodules uvicorn --collect-submodules djlib \
  --collect-data keyring --copy-metadata keyring \
  --distpath dist --workpath build packaging/app_entry.py
echo "Built: $(pwd)/dist/DJ Library.app"
