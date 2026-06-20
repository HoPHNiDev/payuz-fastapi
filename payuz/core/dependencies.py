"""
Dependency checker for the payuz FastAPI integration.
"""
import warnings
from typing import List

_FASTAPI_PACKAGES = ["fastapi", "sqlalchemy", "httpx", "pydantic"]
_MANUAL_INSTALL = "pip install fastapi sqlalchemy httpx pydantic python-multipart"


class DependencyError(ImportError):
    """Raised when required dependencies are missing."""


def get_missing_dependencies(framework: str = "fastapi") -> List[str]:
    """Return the list of missing packages for the FastAPI integration."""
    if framework != "fastapi":
        raise ValueError(f"Unsupported framework: {framework} (payuz is FastAPI-only)")
    missing = []
    for package in _FASTAPI_PACKAGES:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)
    return missing


def check_dependencies(framework: str = "fastapi", raise_error: bool = False) -> bool:
    """Check that the FastAPI integration's dependencies are installed."""
    missing = get_missing_dependencies(framework)
    if not missing:
        return True
    msg = (
        f"payuz: missing dependencies: {', '.join(missing)}.\n"
        f"Install with:  pip install 'payuz-fastapi'  (or: {_MANUAL_INSTALL})"
    )
    if raise_error:
        raise DependencyError(msg)
    warnings.warn(msg, ImportWarning, stacklevel=2)
    return False


def require_framework(framework: str = "fastapi"):
    """Decorator that checks dependencies before calling the wrapped function."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            check_dependencies(framework, raise_error=True)
            return func(*args, **kwargs)
        return wrapper
    return decorator
