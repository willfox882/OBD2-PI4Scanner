class OBDError(Exception):
    """Base exception for OBD related errors."""
    pass

class SafetyInterlock(OBDError):
    """Raised when a command is blocked by safety protocols."""
    pass

class CommandNotSupportedError(OBDError):
    """Raised when a module rejects a command (e.g. NRC 0x12 or 0x31)."""
    pass

class ConnectionError(OBDError):
    """Raised when communication with the adapter fails."""
    pass
