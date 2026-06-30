# Installation

## From the beta wheel

Direct GitHub beta pre-release install:

```powershell
python -m pip install --force-reinstall "https://github.com/Nidhish2007/AgentSeal-/releases/download/v5.0.0-beta.2/agentseal-5.0.0-1beta2fix1-py3-none-any.whl"
agentseal
```

Windows / PowerShell:

```powershell
cd "$env:USERPROFILE\Downloads"
python -m pip install --upgrade pip
python -m pip install --force-reinstall ".\agentseal-5.0.0-1beta2fix1-py3-none-any.whl"
agentseal
```

macOS / Linux:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --force-reinstall ./agentseal-5.0.0-1beta2fix1-py3-none-any.whl
agentseal
```

## From source

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .[dev]
python scripts/smoke_test.py
```

The source tree does not include large beta artifacts. Install the release wheel for the full offline artifact set.
