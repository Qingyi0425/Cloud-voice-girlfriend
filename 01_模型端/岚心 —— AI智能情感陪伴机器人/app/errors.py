class ProviderError(Exception):
    """An expected upstream provider failure safe to show as a Chinese message."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class ConfigurationError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, status_code=503)

