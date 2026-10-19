class DatabaseError(Exception):
    """Base error for the teaching database's public API."""


class ValidationError(DatabaseError, ValueError):
    pass


class NotFoundError(DatabaseError, LookupError):
    pass


class AlreadyExistsError(DatabaseError):
    pass


class CorruptionError(DatabaseError):
    pass


class PageFullError(DatabaseError):
    pass


class LegacyFormatError(DatabaseError):
    pass


class BufferClosedError(DatabaseError, RuntimeError):
    pass
