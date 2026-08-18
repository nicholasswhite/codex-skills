"""User-facing exception hierarchy for the transcription pipeline."""


class TranscribeAnythingError(Exception):
    """Base exception whose message is safe to show to a local user."""


class ConfigurationError(TranscribeAnythingError):
    """Raised when a required runtime setting or dependency is unavailable."""


class SourceError(TranscribeAnythingError):
    """Raised when input media cannot be found, fetched, or validated."""


class SecurityError(SourceError):
    """Raised when a source violates the URL or filesystem safety policy."""


class MediaError(TranscribeAnythingError):
    """Raised when media inspection, decoding, or normalization fails."""


class ProviderError(TranscribeAnythingError):
    """Raised when a transcription provider cannot complete the request."""


class OutputError(TranscribeAnythingError):
    """Raised when transcript artifacts cannot be rendered or written."""
