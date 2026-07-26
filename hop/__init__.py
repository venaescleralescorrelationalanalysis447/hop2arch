"""hop2arch — move a Windows setup onto Arch Linux without losing the thread.

Three verbs, in order:

    hop scan     runs on Windows (windows/hop-scan.ps1) and writes hopfile.json
    hop plan     turns a hopfile into a package plan and a human-readable report
    hop land     executes that plan on the freshly installed Arch box

Everything in between (`hop install-config`, `hop diff`, `hop doctor`,
`hop scrub`, `hop db`) exists to make those three survive contact with a real
machine.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
