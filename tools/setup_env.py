"""Create .venv and install bpmn8n dependencies using the Python running this file."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="Include the test/browser dependencies")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required. Python 3.12 is recommended.")

    root = Path(__file__).resolve().parent.parent
    environment = root / ".venv"
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not executable.exists():
        venv.EnvBuilder(with_pip=True).create(environment)

    requirements = root / ("requirements-dev.txt" if args.dev else "requirements.txt")
    subprocess.run([str(executable), "-m", "pip", "install", "-r", str(requirements)], cwd=root, check=True)
    print(f"\nEnvironment ready: {environment}")
    print(f'Run Django with: "{executable}" manage.py runserver')
    if args.dev:
        print(f'For browser tests, install Chromium with: "{executable}" -m playwright install chromium')


if __name__ == "__main__":
    main()
