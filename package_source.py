"""Bundle source and infrastructure, excluding secrets, runtime data and environments."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent
destination = root.parent / 'release-command-center.zip'
excluded = {'.venv', 'data', '__pycache__', '.pytest_cache', '.git'}
with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if path.is_file() and not any(part in excluded for part in relative.parts) and path.name != '.env':
            archive.write(path, Path(root.name) / relative)
print(destination)
